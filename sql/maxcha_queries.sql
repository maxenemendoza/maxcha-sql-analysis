-- =====================================================================
-- MAXCHA: unit economics and pricing analysis (PostgreSQL)
-- Database: maxcha
-- Run order: views first, then the checks, then Q1-Q9, then the extras.
-- Tip: in DBeaver, press Alt+X to run the whole script, or click inside
-- one query and press Ctrl+Enter to run just that query.
-- =====================================================================


-- =====================================================================
-- 00. VIEWS (shortcuts that hold the revenue and cost math)
-- Safe to re-run: CREATE OR REPLACE VIEW updates them if they exist.
-- =====================================================================

-- Cost of one cup of each drink: recipe quantities x ingredient cost, plus 5% wastage
CREATE OR REPLACE VIEW drink_cost AS
SELECT r.drink_id,
       SUM(r.qty * i.unit_cost) * 1.05 AS cogs_per_cup
FROM recipe_items r
JOIN ingredients i ON i.item_code = r.item_code
GROUP BY r.drink_id;

-- One row per order with price, cost, revenue and profit (add-ons included)
CREATE OR REPLACE VIEW order_lines AS
WITH addon AS (
  SELECT oa.order_id,
         SUM(oa.units_per_cup * ad.price_per_unit) AS addon_price,
         SUM(oa.units_per_cup * ad.cost_per_unit)  AS addon_cost
  FROM order_addons oa
  JOIN addons ad ON ad.addon_id = oa.addon_id
  GROUP BY oa.order_id
),
base AS (
  SELECT o.order_id, o.order_date, o.customer_id,
         d.flavor, d.format, d.cup_size,
         o.qty_cups, o.unit_price,
         COALESCE(a.addon_price, 0) AS addon_price,
         COALESCE(a.addon_cost, 0)  AS addon_cost,
         (a.order_id IS NOT NULL)   AS has_addon,
         dc.cogs_per_cup            AS drink_cost
  FROM orders o
  JOIN drinks d      ON d.drink_id  = o.drink_id
  JOIN drink_cost dc ON dc.drink_id = o.drink_id
  LEFT JOIN addon a  ON a.order_id  = o.order_id
)
SELECT *,
       unit_price + addon_price                                         AS price_per_cup,
       drink_cost + addon_cost                                          AS cost_per_cup,
       qty_cups * (unit_price + addon_price)                            AS revenue,
       qty_cups * (drink_cost + addon_cost)                             AS cogs,
       qty_cups * (unit_price + addon_price - drink_cost - addon_cost)  AS gross_profit
FROM base;


-- =====================================================================
-- 01. DATA CHECKS
-- =====================================================================

-- Totals should match the old workbook: 38 orders, 50 cups, 7,062 revenue
-- (COGS is about 3,050.50; the old workbook said 3,074.95, a gap under 1%)
SELECT COUNT(*)                 AS order_lines,
       SUM(qty_cups)            AS cups,
       ROUND(SUM(revenue), 2)   AS revenue,
       ROUND(SUM(cogs), 2)      AS cogs,
       ROUND(SUM(gross_profit), 2) AS gross_profit
FROM order_lines;

-- Every drink that was sold must have a recipe. Expected: no rows.
SELECT DISTINCT d.flavor, d.format, d.cup_size
FROM orders o
JOIN drinks d ON d.drink_id = o.drink_id
WHERE d.drink_id NOT IN (SELECT drink_id FROM recipe_items);


-- =====================================================================
-- Q1. What is the profit per cup and total profit for each drink,
--     and what is the margin?
-- =====================================================================
SELECT flavor, format, cup_size,
       SUM(qty_cups)                                         AS cups,
       ROUND(SUM(revenue), 2)                                AS revenue,
       ROUND(SUM(gross_profit), 2)                           AS total_profit,
       ROUND(SUM(gross_profit) / SUM(qty_cups), 2)           AS profit_per_cup,
       ROUND(SUM(gross_profit) / SUM(revenue) * 100, 2)      AS margin_pct
FROM order_lines
GROUP BY flavor, format, cup_size
ORDER BY total_profit DESC;


-- =====================================================================
-- Q2. 12 oz vs 16 oz: which size earns more per cup at TODAY'S menu prices?
--     (A what-if: no orders exist yet at the new prices.)
-- =====================================================================
SELECT d.flavor, d.cup_size, d.menu_price,
       ROUND(c.cogs_per_cup, 2)                                       AS cogs,
       ROUND(d.menu_price - c.cogs_per_cup, 2)                        AS profit_per_cup,
       ROUND((d.menu_price - c.cogs_per_cup) / d.menu_price * 100, 1) AS margin_pct
FROM drinks d
JOIN drink_cost c ON c.drink_id = d.drink_id
WHERE d.format = 'Iced'
ORDER BY d.flavor, d.cup_size;


-- =====================================================================
-- Q3. Do cups with an add-on earn more profit than plain cups?
-- =====================================================================
SELECT has_addon,
       SUM(qty_cups)                                   AS cups,
       ROUND(SUM(revenue) / SUM(qty_cups), 2)          AS avg_price_per_cup,
       ROUND(SUM(gross_profit) / SUM(qty_cups), 2)     AS avg_profit_per_cup,
       ROUND(SUM(gross_profit) / SUM(revenue) * 100, 2) AS margin_pct
FROM order_lines
GROUP BY has_addon;


-- =====================================================================
-- Q4. How many cups to break even on operating expenses?
--     Have I sold enough?
-- Operating expenses exclude Ingredient/Packaging Restock, because the
-- cost of the ingredients used is already counted in COGS.
-- =====================================================================
SELECT cups_sold,
       avg_profit_per_cup,
       operating_expenses,
       ROUND(operating_expenses / avg_profit_per_cup, 1) AS break_even_cups,
       cups_sold - ROUND(operating_expenses / avg_profit_per_cup, 1) AS cups_above_break_even
FROM (
  SELECT
    (SELECT SUM(qty_cups) FROM order_lines)                                AS cups_sold,
    (SELECT ROUND(SUM(gross_profit) / SUM(qty_cups), 2) FROM order_lines)  AS avg_profit_per_cup,
    (SELECT SUM(amount) FROM expenses
     WHERE category IN ('Equipment', 'R&D', 'Marketing', 'Delivery Fees')) AS operating_expenses
) t;


-- =====================================================================
-- Q5. What makes up the cost of a cup? (Iced Matcha 12 oz)
-- The item costs add up to about 51.14; with 5% wastage that is 53.70.
-- =====================================================================
SELECT i.item_name,
       i.item_type,
       r.qty,
       ROUND(r.qty * i.unit_cost, 2) AS cost_per_cup
FROM recipe_items r
JOIN ingredients i ON i.item_code = r.item_code
JOIN drinks d      ON d.drink_id  = r.drink_id
WHERE d.flavor = 'Matcha' AND d.format = 'Iced' AND d.cup_size = '12 oz'
ORDER BY cost_per_cup DESC;


-- =====================================================================
-- Q6. How did sales change week by week?
-- (Add what was happening each week by hand in the README.)
-- =====================================================================
SELECT DATE_TRUNC('week', order_date)::date AS week_start,
       COUNT(*)                              AS orders,
       SUM(qty_cups)                         AS cups,
       ROUND(SUM(revenue), 2)                AS revenue,
       ROUND(SUM(revenue) - SUM(cogs), 2)    AS gross_profit
FROM order_lines
GROUP BY DATE_TRUNC('week', order_date)::date
ORDER BY week_start;


-- =====================================================================
-- Q9. What is my net profit to date after operating expenses?
-- Expected: revenue 7,062 | gross profit 4,011.50 | operating expenses 2,064
--           net profit 1,947.50 | net margin 27.6%
-- =====================================================================
SELECT revenue,
       gross_profit,
       operating_expenses,
       ROUND(gross_profit - operating_expenses, 2)                   AS net_profit,
       ROUND((gross_profit - operating_expenses) / revenue * 100, 1) AS net_margin_pct
FROM (
  SELECT
    (SELECT ROUND(SUM(revenue), 2)      FROM order_lines) AS revenue,
    (SELECT ROUND(SUM(gross_profit), 2) FROM order_lines) AS gross_profit,
    (SELECT SUM(amount) FROM expenses
     WHERE category IN ('Equipment', 'R&D', 'Marketing', 'Delivery Fees')) AS operating_expenses
) t;


-- =====================================================================
-- EXTRA A. Revenue and cups by format (Iced vs Bottled)
-- Expected: Iced 6,624 / 48 cups | Bottled 438 / 2 cups
-- =====================================================================
SELECT format,
       ROUND(SUM(revenue), 2) AS total_revenue,
       SUM(qty_cups)          AS cups
FROM order_lines
GROUP BY format
ORDER BY total_revenue DESC;


-- =====================================================================
-- EXTRA B. Top 5 customers by revenue
-- =====================================================================
SELECT customer_id,
       ROUND(SUM(revenue), 2) AS total_revenue,
       SUM(qty_cups)          AS cups
FROM order_lines
GROUP BY customer_id
ORDER BY total_revenue DESC, cups DESC
LIMIT 5;


-- ============================================================
-- View used by the Power BI dashboard (Q2 as a view)
-- 12 oz vs 16 oz profit per cup at today's menu prices
-- ============================================================
CREATE OR REPLACE VIEW size_whatif AS
SELECT d.flavor, d.cup_size, d.menu_price,
       ROUND(c.cogs_per_cup, 2) AS cogs,
       ROUND(d.menu_price - c.cogs_per_cup, 2) AS profit_per_cup,
       ROUND((d.menu_price - c.cogs_per_cup) / d.menu_price * 100, 1) AS margin_pct
FROM drinks d
JOIN drink_cost c ON c.drink_id = d.drink_id
WHERE d.format = 'Iced';
