"""Monthly report — 09:00 on the 1st, previous month, unconditional (§12).

The report carries the previous month's income, expenses, balance, and the top
spending categories. Like the evening summary it always sends — §12 marks only
the noon nudge as suppressible. The previous-month range is decided once, in
`Asia/Kolkata`: bucketing on the UTC date would cut the month at 05:30 IST and
push the last 5.5 hours of the 31st into the wrong report (§10). Each send is
recorded in `reminder_log` (`kind = 'monthly'`). The `cron` service runs
`python -m kanakko.jobs.monthly` at 09:00 IST on the 1st; the crontab belongs to
a later task.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from kanakko import configure_logging
from kanakko.db import all_users, connect, log_reminder, month_summary
from kanakko.jobs import fan_out
from kanakko.jobs.evening import IST
from kanakko.money import format_amount
from kanakko.tg import send_message

log = logging.getLogger(__name__)

TOP_N = 5


def previous_month_ist(now: datetime | None = None) -> tuple[date, date]:
    """`(first_of_prev_month, first_of_this_month)` in `Asia/Kolkata` (§10).

    The half-open range the report buckets on. Computing it in IST is the whole
    point: at the 09:00 IST run the two are unambiguous, but pinning to the zone
    also keeps the boundary correct for any instant near midnight IST — a UTC-date
    computation would slide it 5.5 hours and, just after IST midnight on the 1st,
    report two months back instead of one. `now` defaults to the current instant.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    this_first = now.date().replace(day=1)
    prev_first = (this_first - timedelta(days=1)).replace(day=1)
    return prev_first, this_first


def report_text(
    month_label: str, income: Decimal, expenses: Decimal, top: list[tuple[str, Decimal]]
) -> str:
    """The message body: month totals, balance, and top spending categories.

    Balance is income − expenses (can be negative — you spent more than you
    earned). `top` is already trimmed to what should be shown; an empty month
    still sends its zeros — the report is unconditional (§12).
    """
    balance = income - expenses
    lines = [
        f"📊 {month_label}",
        f"Income: {format_amount(income)}",
        f"Expenses: {format_amount(expenses)}",
        f"Balance: {format_amount(balance)}",
    ]
    if top:
        lines.append("Top spending:")
        lines += [f"• {category}: {format_amount(amount)}" for category, amount in top]
    return "\n".join(lines)


def run(conn) -> int:
    """Send each user their previous-month report; return how many were sent."""
    first, next_first = previous_month_ist()
    label = first.strftime("%B %Y")

    def deliver(user_id: int, telegram_user_id: int) -> bool:
        income, expenses, top = month_summary(conn, user_id, first, next_first)
        send_message(telegram_user_id, report_text(label, income, expenses, top[:TOP_N]))
        log_reminder(conn, user_id, "monthly")
        return True

    return fan_out(conn, all_users(conn), deliver)


def main() -> None:
    """Cron entry point: `python -m kanakko.jobs.monthly`."""
    configure_logging()
    with connect() as conn:
        sent = run(conn)
    log.info("monthly report sent to %d user(s)", sent)


if __name__ == "__main__":
    main()
