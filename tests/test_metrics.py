"""Tests for compute_metrics(). They use a small hand-made table, so no database is needed."""

from datetime import date

import pandas as pd

from weekly_report import build_summary, compute_metrics, money, pct_change

TODAY = date(2026, 12, 1)  # a fixed "today" so the tests always give the same result


def make_orders(rows: list[tuple]) -> pd.DataFrame:
    """Build an orders table with the same columns the script loads from order_lines."""
    columns = ["order_date", "flavor", "format", "cup_size", "qty_cups",
               "has_addon", "revenue", "cogs"]
    orders = pd.DataFrame(rows, columns=columns)
    orders["gross_profit"] = orders["revenue"] - orders["cogs"]
    return orders


# Week of Mon 26 Oct and week of Mon 2 Nov 2026 (no orders in the week of 9 Nov).
ORDERS = make_orders([
    (date(2026, 10, 26), "Matcha", "Iced", "12 oz", 2, False, 300.0, 120.0),   # Monday
    (date(2026, 11, 1), "Hojicha", "Iced", "16 oz", 2, True, 360.0, 160.0),    # Sunday, same week
    (date(2026, 11, 2), "Matcha", "Iced", "12 oz", 3, False, 450.0, 180.0),    # next Monday
    (date(2026, 11, 8), "Hojicha", "Iced", "16 oz", 1, True, 180.0, 80.0),     # Sunday
])
EXPENSES = pd.DataFrame({
    "expense_date": [date(2026, 9, 20), date(2026, 11, 5), date(2026, 11, 20)],
    "category": ["Equipment", "Marketing", "Equipment"],
    "amount": [500.0, 100.0, 999.0],
})


def test_week_boundaries_monday_to_sunday():
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 2), today=TODAY)
    assert m["week_start"] == date(2026, 11, 2)
    assert m["week_end"] == date(2026, 11, 8)
    # Sunday 1 Nov belongs to the week before; Sunday 8 Nov belongs to this week.
    assert m["this_week"]["orders"] == 2
    assert m["this_week"]["cups"] == 4
    assert m["prev_week"]["cups"] == 4
    assert m["this_week"]["gross_profit"] == 370.0


def test_any_day_picks_its_monday_week():
    # A Wednesday and a Sunday both point to the week starting Monday 2 Nov.
    wednesday = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 4), today=TODAY)
    sunday = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 8), today=TODAY)
    assert wednesday["week_start"] == sunday["week_start"] == date(2026, 11, 2)


def test_metrics_for_a_normal_week():
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 2), today=TODAY)
    assert round(m["this_week"]["gross_margin"], 1) == 58.7   # 370 / 630
    assert m["changes"]["cups"] == 0.0
    assert list(m["top_drinks"]["drink"]) == ["Matcha Iced 12 oz", "Hojicha Iced 16 oz"]
    assert m["size_share"] == {"12 oz": 75.0, "16 oz": 25.0}
    assert m["addon_share"] == 25.0


def test_week_with_no_orders():
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 9), today=TODAY)
    assert m["this_week"]["orders"] == 0
    assert m["this_week"]["cups"] == 0
    assert m["this_week"]["gross_profit"] == 0
    assert m["this_week"]["gross_margin"] is None
    assert m["top_drinks"].empty
    assert m["addon_share"] is None
    assert m["changes"]["cups"] == -100.0
    # The weekly table still lists the empty week, so the chart has a bar for it.
    assert list(m["weekly"]["cups"]) == [4, 4, 0]
    assert "There were no orders this week." in build_summary(m)


def test_percent_change_when_previous_week_is_zero():
    assert pct_change(0, 5) is None
    assert pct_change(0, 0) is None
    assert pct_change(4, 7) == 75.0
    # Week of 26 Oct comes after a week with no orders.
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 10, 26), today=TODAY)
    assert m["changes"]["cups"] is None
    assert build_summary(m)[0] == "Cups sold rose from 0 to 4 (no sales the week before)."


def test_year_to_date_uses_expenses_up_to_week_end():
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 2), today=TODAY)
    ytd = m["ytd"]
    assert ytd["revenue"] == 1290.0
    assert ytd["gross_profit"] == 750.0
    assert ytd["operating_expenses"] == 600.0     # the 20 Nov expense is after this week
    assert ytd["net_profit"] == 150.0
    assert round(ytd["net_margin"], 1) == 11.6


def test_in_progress_week_is_flagged():
    m = compute_metrics(ORDERS, EXPENSES, date(2026, 11, 2), today=date(2026, 11, 5))
    assert m["in_progress"] is True


def test_money_format():
    assert money(1947.5) == "₱1,947.50"
    assert money(-12) == "-₱12.00"
