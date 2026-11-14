"""Build Maxcha's weekly sales report from the PostgreSQL database.

How to run (from the project folder):
    python report/weekly_report.py                    # latest full week with orders
    python report/weekly_report.py --week 2026-10-28  # the week that contains this date
    python report/weekly_report.py --all              # one report for every week to date

Output goes to report/output/: a Markdown report, two PNG charts and an Excel file.
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # save charts to files without opening a window
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
from dotenv import load_dotenv

REPORT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = REPORT_DIR / "output"
ENV_FILE = REPORT_DIR.parent / ".env"

# --- SQL queries (read-only: they never change the tables or views) ---

# One row per order. customer_id is left out on purpose (privacy).
ORDERS_SQL = """
SELECT order_id, order_date, flavor, format, cup_size, qty_cups,
       price_per_cup, cost_per_cup, has_addon, revenue, cogs, gross_profit
FROM order_lines
ORDER BY order_date, order_id;
"""

# Operating expenses only. Restocks are left out because they are already in COGS.
EXPENSES_SQL = """
SELECT expense_date, category, amount
FROM expenses
WHERE category IN ('Equipment', 'R&D', 'Marketing', 'Delivery Fees')
ORDER BY expense_date;
"""

# Same check as in sql/maxcha_queries.sql: every sold drink needs a recipe. Expected: no rows.
MISSING_RECIPE_SQL = """
SELECT DISTINCT d.flavor, d.format, d.cup_size
FROM orders o
JOIN drinks d ON d.drink_id = o.drink_id
WHERE d.drink_id NOT IN (SELECT drink_id FROM recipe_items);
"""

# All-time totals I validated against my original Excel workbook.
# When new orders are added, update these numbers, or set this to None to skip the check.
EXPECTED_TOTALS: dict[str, float] | None = {"orders": 38, "cups": 50, "revenue": 7062.00}

MONEY_COLUMNS = ["price_per_cup", "cost_per_cup", "revenue", "cogs", "gross_profit"]
TOTAL_KEYS = ["orders", "cups", "revenue", "cogs", "gross_profit"]

# Chart colors: one accent for the week in focus, a soft gray for everything else.
ACCENT = "#2a78d6"
MUTED = "#c3c2b7"
TEXT = "#0b0b0b"
TEXT_SOFT = "#52514e"
BACKGROUND = "#fcfcfb"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def week_start_of(day: date) -> date:
    """Return the Monday of the week that contains `day` (same as DATE_TRUNC('week'))."""
    return day - timedelta(days=day.weekday())


def pct_change(old: float, new: float) -> float | None:
    """Percent change from old to new. Returns None when old is 0 (no base to compare)."""
    if old == 0:
        return None
    return (new - old) / old * 100


def pct_of(part: float, whole: float) -> float | None:
    """Return part as a percent of whole, or None when whole is 0."""
    if whole == 0:
        return None
    return part / whole * 100


def money(amount: float) -> str:
    """Format a number as Philippine pesos with 2 decimals, e.g. ₱1,947.50."""
    amount = round(amount, 2)
    sign = "-" if amount < 0 else ""
    return f"{sign}₱{abs(amount):,.2f}"


def percent(value: float | None) -> str:
    """Format a percent with 1 decimal, e.g. 27.6%. Shows 'n/a' when there is no value."""
    return "n/a" if value is None else f"{value:.1f}%"


def signed_percent(value: float | None) -> str:
    """Format a percent change with a + or - sign, e.g. +75.0%."""
    return "n/a" if value is None else f"{value:+.1f}%"


def order_dates(orders: pd.DataFrame) -> pd.Series:
    """Return the order dates as plain Python dates, so they compare with date objects."""
    return pd.to_datetime(orders["order_date"]).dt.date


def rows_between(orders: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Return the orders dated from start to end, both days included."""
    dates = order_dates(orders)
    return orders[(dates >= start) & (dates <= end)]


def totals(rows: pd.DataFrame) -> dict:
    """Add up orders, cups, revenue, COGS and gross profit for some order rows."""
    revenue = float(rows["revenue"].sum())
    gross_profit = float(rows["gross_profit"].sum())
    return {
        "orders": len(rows),
        "cups": int(rows["qty_cups"].sum()),
        "revenue": revenue,
        "cogs": float(rows["cogs"].sum()),
        "gross_profit": gross_profit,
        "gross_margin": pct_of(gross_profit, revenue),
    }


def run_query(conn: psycopg.Connection, sql: str) -> pd.DataFrame:
    """Run one SQL query and return the result as a DataFrame."""
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [col.name for col in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


# ---------------------------------------------------------------------------
# Main steps
# ---------------------------------------------------------------------------

def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Connect to PostgreSQL using .env and return (orders, operating expenses, recipe problems)."""
    load_dotenv(ENV_FILE)
    settings = {
        "host": os.getenv("PGHOST"),
        "port": os.getenv("PGPORT"),
        "dbname": os.getenv("PGDATABASE"),
        "user": os.getenv("PGUSER"),
        "password": os.getenv("PGPASSWORD"),
    }
    missing = [key for key, value in settings.items() if not value]
    if missing:
        sys.exit(f"Missing database settings in .env: {', '.join(missing)}. "
                 "Copy .env.example to .env and fill it in.")

    try:
        with psycopg.connect(**settings) as conn:
            orders = run_query(conn, ORDERS_SQL)
            expenses = run_query(conn, EXPENSES_SQL)
            recipe_problems = run_query(conn, MISSING_RECIPE_SQL)
    except psycopg.OperationalError as error:
        sys.exit(f"Could not connect to the database. Check your .env settings.\n{error}")

    # PostgreSQL returns NUMERIC columns as Decimal; turn them into normal floats.
    # errors="coerce" keeps missing values as NaN so run_checks() can catch them.
    for column in MONEY_COLUMNS:
        orders[column] = pd.to_numeric(orders[column], errors="coerce").astype(float)
    expenses["amount"] = pd.to_numeric(expenses["amount"]).astype(float)
    return orders, expenses, recipe_problems


def run_checks(orders: pd.DataFrame, recipe_problems: pd.DataFrame) -> None:
    """Check the data before writing anything. Stop with a clear message if a check fails."""
    problems = []

    if "customer_id" in orders.columns:
        problems.append("Customer data was loaded. Remove customer_id from the order_lines query.")

    null_rows = orders[MONEY_COLUMNS].isna().any(axis=1).sum()
    if null_rows:
        problems.append(f"{null_rows} order rows have a missing price or cost.")

    if not recipe_problems.empty:
        names = ", ".join(" ".join(map(str, row)) for row in recipe_problems.itertuples(index=False))
        problems.append(f"These sold drinks have no recipe: {names}.")

    if EXPECTED_TOTALS is not None:
        actual = {
            "orders": len(orders),
            "cups": int(orders["qty_cups"].sum()),
            "revenue": round(float(orders["revenue"].sum()), 2),
        }
        for key, expected in EXPECTED_TOTALS.items():
            if actual[key] != expected:
                problems.append(f"All-time {key} is {actual[key]}, expected {expected}. "
                                "If you added new orders, update EXPECTED_TOTALS.")

    if problems:
        print("Data checks failed. Nothing was written.")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("Data checks passed.")


def weekly_table(orders: pd.DataFrame, last_week: date) -> pd.DataFrame:
    """One row per week, from the first week with orders up to last_week (empty weeks included)."""
    if orders.empty:
        first_week = last_week
    else:
        first_week = min(week_start_of(min(order_dates(orders))), last_week)

    rows = []
    previous = None  # last week's totals; None for the very first week
    week = first_week
    while week <= last_week:
        week_rows = rows_between(orders, week, week + timedelta(days=6))
        row = {"week_start": week, **totals(week_rows)}

        # Compare with last week (no comparison for the first week).
        if previous is None:
            row["cups_change_pct"] = None
            row["gross_profit_change_pct"] = None
        else:
            row["cups_change_pct"] = pct_change(previous["cups"], row["cups"])
            row["gross_profit_change_pct"] = pct_change(previous["gross_profit"], row["gross_profit"])

        rows.append(row)
        previous = row
        week += timedelta(days=7)

    return pd.DataFrame(rows)


def compute_metrics(orders: pd.DataFrame, expenses: pd.DataFrame, week: date,
                    today: date | None = None) -> dict:
    """Work out every number in the report for the week that contains `week`."""
    today = today or date.today()
    week_start = week_start_of(week)
    week_end = week_start + timedelta(days=6)
    prev_start = week_start - timedelta(days=7)

    week_rows = rows_between(orders, week_start, week_end)
    this_week = totals(week_rows)
    prev_week = totals(rows_between(orders, prev_start, prev_start + timedelta(days=6)))
    changes = {key: pct_change(prev_week[key], this_week[key]) for key in TOTAL_KEYS}

    # Drinks ranked by gross profit this week (like GROUP BY ... ORDER BY in SQL).
    drink_columns = ["flavor", "format", "cup_size"]
    drinks = week_rows.groupby(drink_columns, as_index=False)[["qty_cups", "revenue", "gross_profit"]].sum()
    drinks = drinks.rename(columns={"qty_cups": "cups"})
    drinks = drinks.sort_values("gross_profit", ascending=False, ignore_index=True)
    drinks["drink"] = drinks["flavor"] + " " + drinks["format"] + " " + drinks["cup_size"]

    # Share of cups by size. 12 oz and 16 oz always show; other sizes only if sold.
    cups = this_week["cups"]
    cups_by_size = week_rows.groupby("cup_size")["qty_cups"].sum()
    sizes = ["12 oz", "16 oz"] + [s for s in cups_by_size.index if s not in ("12 oz", "16 oz")]
    size_share = {size: pct_of(int(cups_by_size.get(size, 0)), cups) for size in sizes}
    addon_cups = int(week_rows.loc[week_rows["has_addon"].astype(bool), "qty_cups"].sum())

    # Year to date: from 1 January up to the end of this week.
    year_start = date(week_end.year, 1, 1)
    ytd = totals(rows_between(orders, year_start, week_end))
    expense_dates = pd.to_datetime(expenses["expense_date"]).dt.date
    in_ytd = (expense_dates >= year_start) & (expense_dates <= week_end)
    ytd["operating_expenses"] = float(expenses.loc[in_ytd, "amount"].sum())
    ytd["net_profit"] = ytd["gross_profit"] - ytd["operating_expenses"]
    ytd["net_margin"] = pct_of(ytd["net_profit"], ytd["revenue"])

    return {
        "week_start": week_start,
        "week_end": week_end,
        "in_progress": week_end >= today,
        "this_week": this_week,
        "prev_week": prev_week,
        "changes": changes,
        "drinks": drinks,
        "top_drinks": drinks.head(3),
        "size_share": size_share,
        "addon_share": pct_of(addon_cups, cups),
        "ytd": ytd,
        "weekly": weekly_table(orders, week_start),
    }


def change_sentence(label: str, old: float, new: float, fmt) -> str:
    """Describe a change in words, e.g. 'Cups sold rose from 4 to 7 (+75.0%).'"""
    if new == old:
        return f"{label} stayed at {fmt(new)}."
    verb = "rose" if new > old else "fell"
    change = pct_change(old, new)
    detail = "no sales the week before" if change is None else signed_percent(change)
    return f"{label} {verb} from {fmt(old)} to {fmt(new)} ({detail})."


def build_summary(m: dict) -> list[str]:
    """Turn the metrics into 3 to 5 plain-language bullet points, using fixed rules."""
    this_week, prev_week, ytd = m["this_week"], m["prev_week"], m["ytd"]
    bullets = [change_sentence("Cups sold", prev_week["cups"], this_week["cups"], str)]

    profit = change_sentence("Gross profit", prev_week["gross_profit"],
                             this_week["gross_profit"], money)
    if this_week["gross_margin"] is not None:
        profit += f" Gross margin was {percent(this_week['gross_margin'])}."
    bullets.append(profit)

    if this_week["cups"] == 0:
        bullets.append("There were no orders this week.")
    else:
        best = m["top_drinks"].iloc[0]
        best_cups = int(best["cups"])
        bullets.append(f"Best earner: {best['drink']}, {money(best['gross_profit'])} "
                       f"gross profit from {best_cups} {'cup' if best_cups == 1 else 'cups'}.")
        # Find the cup size with the biggest share.
        top_size, top_share = None, -1.0
        for size, share in m["size_share"].items():
            if share > top_share:
                top_size, top_share = size, share
        bullets.append(f"Most cups were {top_size} ({percent(m['size_share'][top_size])}), "
                       f"and {percent(m['addon_share'])} of cups had an add-on.")

    result = "net profit" if ytd["net_profit"] >= 0 else "net loss"
    bullets.append(f"Year to date: {money(ytd['revenue'])} revenue and "
                   f"{money(abs(ytd['net_profit']))} {result} "
                   f"({percent(ytd['net_margin'])} net margin) after "
                   f"{money(ytd['operating_expenses'])} in operating expenses.")
    return bullets


def style_axes(ax: plt.Axes) -> None:
    """Give a chart quiet axes: no top/right border, soft gray text and grid."""
    ax.set_facecolor(BACKGROUND)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=TEXT_SOFT)
    ax.set_axisbelow(True)


def make_charts(m: dict, out_dir: Path) -> dict[str, Path]:
    """Save the two PNG charts for this week and return their file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"maxcha_weekly_{m['week_start']}"
    paths = {"trend": out_dir / f"{stem}_gross_profit.png",
             "drinks": out_dir / f"{stem}_top_drinks.png"}

    # Chart 1: weekly gross profit, with the selected week highlighted.
    weekly = m["weekly"]
    fig, ax = plt.subplots(figsize=(8, 4), facecolor=BACKGROUND)
    style_axes(ax)
    labels = [f"{w.day} {w:%b}" for w in weekly["week_start"]]
    colors = [ACCENT if w == m["week_start"] else MUTED for w in weekly["week_start"]]
    bars = ax.bar(labels, weekly["gross_profit"], color=colors, width=0.7)
    selected = bars[len(bars) - 1]
    ax.annotate(money(selected.get_height()),
                (selected.get_x() + selected.get_width() / 2, selected.get_height()),
                xytext=(0, 4), textcoords="offset points", ha="center", color=TEXT)
    ax.set_title(f"Weekly gross profit (week of {m['week_start']:%d %b %Y} in blue)",
                 loc="left", color=TEXT)
    ax.set_ylabel("Gross profit (₱)", color=TEXT_SOFT)
    ax.yaxis.grid(True, color="#e6e5e0")
    if len(labels) > 12:
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(paths["trend"], dpi=150)
    plt.close(fig)

    # Chart 2: top drinks by gross profit this week (up to 5).
    top = m["drinks"].head(5).iloc[::-1]  # reversed so the biggest bar is on top
    fig, ax = plt.subplots(figsize=(8, 3.5), facecolor=BACKGROUND)
    style_axes(ax)
    ax.set_title(f"Top drinks by gross profit, week of {m['week_start']:%d %b %Y}",
                 loc="left", color=TEXT)
    if top.empty:
        ax.text(0.5, 0.5, "No orders this week", ha="center", va="center",
                color=TEXT_SOFT, transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        ax.barh(top["drink"], top["gross_profit"], color=ACCENT, height=0.6)
        for y, value in enumerate(top["gross_profit"]):
            ax.annotate(money(value), (value, y), xytext=(4, 0), textcoords="offset points",
                        va="center", color=TEXT)
        ax.set_xlabel("Gross profit (₱)", color=TEXT_SOFT)
        ax.set_xlim(0, top["gross_profit"].max() * 1.25)
        ax.xaxis.grid(True, color="#e6e5e0")
    fig.tight_layout()
    fig.savefig(paths["drinks"], dpi=150)
    plt.close(fig)
    return paths


def write_report(m: dict, charts: dict[str, Path], out_dir: Path) -> tuple[Path, Path]:
    """Write the Markdown report and the Excel weekly table. Return both file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"maxcha_weekly_{m['week_start']}"
    md_path = out_dir / f"{stem}.md"
    xlsx_path = out_dir / f"{stem}.xlsx"
    this_week, prev_week, changes, ytd = m["this_week"], m["prev_week"], m["changes"], m["ytd"]

    status = " (week in progress)" if m["in_progress"] else ""
    lines = [
        f"# Maxcha weekly report: {m['week_start']:%d %b} to {m['week_end']:%d %b %Y}{status}",
        "",
        "## Summary",
        "",
        *[f"- {bullet}" for bullet in build_summary(m)],
        "",
        "## This week vs last week",
        "",
        "| Metric | This week | Last week | Change |",
        "|---|---:|---:|---:|",
        f"| Orders | {this_week['orders']} | {prev_week['orders']} | {signed_percent(changes['orders'])} |",
        f"| Cups sold | {this_week['cups']} | {prev_week['cups']} | {signed_percent(changes['cups'])} |",
    ]
    for key, label in [("revenue", "Revenue"), ("cogs", "COGS"), ("gross_profit", "Gross profit")]:
        lines.append(f"| {label} | {money(this_week[key])} | {money(prev_week[key])} "
                     f"| {signed_percent(changes[key])} |")
    lines.append(f"| Gross margin | {percent(this_week['gross_margin'])} "
                 f"| {percent(prev_week['gross_margin'])} | |")

    lines += ["", "## Top 3 drinks by gross profit", ""]
    if m["top_drinks"].empty:
        lines.append("No orders this week.")
    else:
        lines += ["| # | Drink | Cups | Revenue | Gross profit |", "|---|---|---:|---:|---:|"]
        for rank, row in enumerate(m["top_drinks"].itertuples(), start=1):
            lines.append(f"| {rank} | {row.drink} | {row.cups} | {money(row.revenue)} "
                         f"| {money(row.gross_profit)} |")

    lines += ["", "## Cup mix", "", "| | Share of cups |", "|---|---:|"]
    for size, share in m["size_share"].items():
        lines.append(f"| {size} | {percent(share)} |")
    lines.append(f"| With an add-on | {percent(m['addon_share'])} |")

    lines += [
        "", f"## Year to date (1 Jan to {m['week_end']:%d %b %Y})", "",
        "| Metric | Amount |", "|---|---:|",
        f"| Orders | {ytd['orders']} |",
        f"| Cups sold | {ytd['cups']} |",
        f"| Revenue | {money(ytd['revenue'])} |",
        f"| COGS | {money(ytd['cogs'])} |",
        f"| Gross profit | {money(ytd['gross_profit'])} |",
        f"| Operating expenses | {money(ytd['operating_expenses'])} |",
        f"| Net profit | {money(ytd['net_profit'])} |",
        f"| Net margin | {percent(ytd['net_margin'])} |",
        "",
        "Operating expenses are Equipment, R&D, Marketing and Delivery Fees. "
        "Restocks are left out because they are already counted in COGS.",
        "",
        "## Charts",
        "",
        f"![Weekly gross profit]({charts['trend'].name})",
        "",
        f"![Top drinks by gross profit]({charts['drinks'].name})",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # Excel: the weekly table and this week's drinks, with readable column names.
    weekly = m["weekly"].rename(columns={
        "week_start": "Week start", "orders": "Orders", "cups": "Cups", "revenue": "Revenue",
        "cogs": "COGS", "gross_profit": "Gross profit", "gross_margin": "Gross margin %",
        "cups_change_pct": "Cups change %", "gross_profit_change_pct": "Gross profit change %",
    }).round(2)
    drinks = m["drinks"][["drink", "cups", "revenue", "gross_profit"]].rename(columns={
        "drink": "Drink", "cups": "Cups", "revenue": "Revenue", "gross_profit": "Gross profit",
    }).round(2)
    with pd.ExcelWriter(xlsx_path) as writer:
        weekly.to_excel(writer, sheet_name="Weekly", index=False)
        drinks.to_excel(writer, sheet_name="Drinks this week", index=False)
    return md_path, xlsx_path


def latest_full_week(orders: pd.DataFrame, today: date) -> date:
    """Return the Monday of the most recent finished week that has orders."""
    weeks = {week_start_of(d) for d in order_dates(orders)}
    finished = [w for w in weeks if w + timedelta(days=6) < today]
    if not finished:
        sys.exit("No finished week with orders yet. Use --week YYYY-MM-DD to pick a week.")
    return max(finished)


def parse_args() -> argparse.Namespace:
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Build Maxcha's weekly sales report.")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--week", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="report on the week (Monday to Sunday) that contains this date")
    choice.add_argument("--all", action="store_true",
                        help="write a report for every week from the first order to today")
    return parser.parse_args()


def main() -> None:
    """Load the data, check it, then write the report for each chosen week."""
    args = parse_args()
    orders, expenses, recipe_problems = load_data()
    run_checks(orders, recipe_problems)
    if orders.empty:
        sys.exit("There are no orders in the database yet.")

    today = date.today()
    if args.all:
        first = week_start_of(min(order_dates(orders)))
        last = week_start_of(max(order_dates(orders)))
        weeks = [first + timedelta(days=7 * i) for i in range((last - first).days // 7 + 1)]
    elif args.week:
        weeks = [week_start_of(args.week)]
    else:
        weeks = [latest_full_week(orders, today)]

    for week in weeks:
        metrics = compute_metrics(orders, expenses, week, today)
        charts = make_charts(metrics, OUTPUT_DIR)
        md_path, xlsx_path = write_report(metrics, charts, OUTPUT_DIR)
        print(f"Week of {week}: wrote {md_path.name}, {xlsx_path.name} and 2 charts")
    print(f"Files are in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
