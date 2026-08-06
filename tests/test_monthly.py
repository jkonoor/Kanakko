"""The monthly report job (§12) — month bucketing, money totals, message shape.

The two silent-failure risks mirror the evening summary's: bucketing the month
in UTC instead of `Asia/Kolkata` (the last 5.5 hours of the 31st land in the
wrong month, §10), and a total that pulls in a soft-deleted row by reading
`transactions` instead of `active_transactions` (§6). Each gets a guard that
reddens if it regresses. `month_summary` reads real Postgres (the `conn`
fixture); the month-boundary and message helpers are pure.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from kanakko.db import get_or_create_user, month_summary
from kanakko.jobs import monthly
from kanakko.jobs.evening import IST
from kanakko.jobs.monthly import previous_month_ist, report_text
from kanakko.migrate import migrate


def _insert(conn, user_id, amount, type_, occurred_on, category=None, deleted=False):
    deleted_at = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc) if deleted else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, amount, type, category, note, occurred_on, deleted_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_id, amount, type_, category, None, occurred_on, deleted_at),
        )


def test_month_summary_buckets_by_ist_month_and_excludes_deleted(conn):
    """Only the month's live rows count: other months and soft-deleted rows are out.

    A read from `transactions` instead of `active_transactions` would fold the
    deleted ₹500 back into July's spend; a range that leaked into August or June
    would pull in the ₹99 / ₹77 boundary rows. Both are silent — a wrong total
    errors nowhere — so both are asserted. The 23:50-on-the-31st row must count.
    """
    migrate(conn)
    first, next_first = date(2026, 7, 1), date(2026, 8, 1)
    user = get_or_create_user(conn, 710710)

    _insert(conn, user, Decimal("120.50"), "expense", date(2026, 7, 15), "Food")
    _insert(conn, user, Decimal("30.00"), "expense", date(2026, 7, 31), "Food")  # last day
    _insert(conn, user, Decimal("200.00"), "expense", date(2026, 7, 20), "Transport")
    _insert(conn, user, Decimal("20000.00"), "income", date(2026, 7, 1), "Salary")
    _insert(conn, user, Decimal("99.00"), "expense", date(2026, 8, 1), "Food")  # next month
    _insert(conn, user, Decimal("77.00"), "expense", date(2026, 6, 30), "Food")  # prev month
    _insert(conn, user, Decimal("500.00"), "expense", date(2026, 7, 10), "Food", deleted=True)

    income, expenses, top = month_summary(conn, user, first, next_first)

    assert income == Decimal("20000.00")
    assert expenses == Decimal("350.50")  # 120.50 + 30.00 + 200.00, deleted 500 excluded
    assert isinstance(income, Decimal) and isinstance(expenses, Decimal)
    # Biggest expense category first; income category (Salary) not in the spend list.
    assert top == [("Transport", Decimal("200.00")), ("Food", Decimal("150.50"))]
    conn.rollback()


def test_month_summary_empty_month(conn):
    """A month with no entries is (0, 0.00, []) — the unconditional report still sends."""
    migrate(conn)
    user = get_or_create_user(conn, 710711)
    income, expenses, top = month_summary(conn, user, date(2026, 7, 1), date(2026, 8, 1))
    assert (income, expenses, top) == (Decimal("0"), Decimal("0"), [])
    conn.rollback()


def test_run_logs_a_monthly_reminder_for_each_user(conn, monkeypatch):
    """Every user the report reaches gets a `reminder_log` 'monthly' row (§12)."""
    migrate(conn)
    a = get_or_create_user(conn, 710800)
    b = get_or_create_user(conn, 710801)
    monkeypatch.setattr(monthly, "send_message", lambda tg_id, text: None)

    sent = monthly.run(conn)

    assert sent == 2
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, kind FROM reminder_log ORDER BY user_id")
        assert cur.fetchall() == [(a, "monthly"), (b, "monthly")]
    conn.rollback()


def test_previous_month_is_computed_in_ist():
    """At the 09:00-on-the-1st run the previous month is unambiguous; year rolls over.

    The boundary must be pinned to IST: 20:00 UTC on Jul 31 is 01:30 IST on Aug 1,
    so the report is July's. A UTC-date computation off Jul 31 would report June.
    """
    run_time = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)  # 14:30 IST, still the 1st
    assert previous_month_ist(run_time) == (date(2026, 7, 1), date(2026, 8, 1))

    # Year rollover: 09:00 IST on Jan 1 reports the previous December.
    jan_first = datetime(2027, 1, 1, 9, 0, tzinfo=IST)
    assert previous_month_ist(jan_first) == (date(2026, 12, 1), date(2027, 1, 1))

    # Just after IST midnight on the 1st, expressed in UTC: must still be July.
    just_after_ist_midnight = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    assert previous_month_ist(just_after_ist_midnight) == (date(2026, 7, 1), date(2026, 8, 1))


def test_report_text():
    """Balance line; top spending listed biggest-first; negative balance; empty month."""
    top = [("Transport", Decimal("200")), ("Food", Decimal("150.50"))]
    assert report_text("July 2026", Decimal("20000"), Decimal("350.50"), top) == (
        "📊 July 2026\n"
        "Income: ₹20,000.00\n"
        "Expenses: ₹350.50\n"
        "Balance: ₹19,649.50\n"
        "Top spending:\n"
        "• Transport: ₹200.00\n"
        "• Food: ₹150.50"
    )
    # Overspent month → negative balance is shown, not hidden.
    assert report_text("July 2026", Decimal("100"), Decimal("500"), []) == (
        "📊 July 2026\nIncome: ₹100.00\nExpenses: ₹500.00\nBalance: ₹-400.00"
    )
    # Empty month: zeros, no top-spending block.
    assert report_text("July 2026", Decimal("0"), Decimal("0"), []) == (
        "📊 July 2026\nIncome: ₹0.00\nExpenses: ₹0.00\nBalance: ₹0.00"
    )
