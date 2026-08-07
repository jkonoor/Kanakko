"""IST calendar ranges the dashboard buckets on (§10).

Every boundary here is computed in `Asia/Kolkata`, never UTC: a UTC-date
computation slides each boundary 5.5 hours, which just after IST midnight puts a
period's opening entries into the previous one.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from kanakko.jobs.evening import IST


def current_month_ist(now: datetime | None = None) -> tuple[date, date]:
    """`(first_of_this_month, first_of_next_month)` in `Asia/Kolkata` (§10).

    The half-open range the dashboard's current-month figures bucket on — the
    mirror of `jobs.monthly.previous_month_ist`, shifted forward one month.
    Computing it in IST is the point: a UTC-date computation would slide the
    boundary 5.5 hours and, just after IST midnight on the 1st, put this month's
    opening entries in last month's figures. `now` defaults to the current instant.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    this_first = now.date().replace(day=1)
    # Day 28 + 4 days always lands in the next month, whatever the length.
    next_first = (this_first + timedelta(days=32)).replace(day=1)
    return this_first, next_first


def current_week_ist(now: datetime | None = None) -> tuple[date, date]:
    """`(monday_of_this_week, next_monday)` in `Asia/Kolkata` (§10, §13).

    The half-open range the dashboard's this-week figures bucket on. Weeks start
    Monday (`date.weekday()`: Mon=0). Computed in IST for the same reason as
    `current_month_ist`: just after IST midnight a UTC-date computation would
    slide the boundary 5.5 hours and, on a Monday, drop the week's opening entries
    into last week. `now` defaults to the current instant.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    monday = now.date() - timedelta(days=now.date().weekday())
    return monday, monday + timedelta(days=7)


@dataclass(frozen=True)
class Period:
    """One selectable time range and its figures — a tab's worth of dashboard.

    Replaces the three stacked Balance/Income/Expenses blocks, which rendered
    nine near-identical rows for three concepts. `key` is the tab id, `short` is
    the tab's own label ("Week" — a segmented control has room for one word),
    `label` names the range inside the panel ("This week", "August 2026", "All
    time"), and `top` is that range's expense categories biggest-first.
    """

    key: str
    short: str
    label: str
    income: Decimal
    expenses: Decimal
    top: list[tuple[str, Decimal]]
    # The same-length period before this one's expenses, for the hero's delta.
    # `None` for a range with no prior period (all-time) — no baseline, no delta.
    prev_expenses: Decimal | None = None
    compare_label: str = ""  # "vs last month" / "vs last week"


def previous_month_first(first_day: date) -> date:
    """First day of the month *before* the one that starts on `first_day` (§10).

    `first_day` is already an `Asia/Kolkata` month boundary (from
    `current_month_ist`), so this is pure calendar arithmetic — no zone. It steps
    back one day into the prior month and truncates to its 1st, never subtracts
    from the month number: the previous month of **January is December of the
    prior year**, and this is the case that catches a naive `month - 1`.
    """
    return (first_day - timedelta(days=1)).replace(day=1)
