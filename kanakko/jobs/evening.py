"""Evening summary — 21:00 daily, unconditional (§12).

Unlike the noon nudge (suppressed when the day already has activity), this one
always sends: it carries the day's total and entry count, so it doubles as the
daily summary even on days you didn't act on it. Each send is recorded in
`reminder_log` (`kind = 'evening'`), which the noon nudge reads to place its
suppression window. The `cron` service runs `python -m kanakko.jobs.evening` at
21:00 `Asia/Kolkata`; the crontab belongs to a later task.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from kanakko import configure_logging
from kanakko.db import all_users, connect, day_summary, log_reminder
from kanakko.money import format_amount
from kanakko.tg import send_message

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def today_ist(now: datetime | None = None) -> date:
    """Today's date in `Asia/Kolkata` (§10).

    The one place the day boundary is decided: bucketing on the UTC date would
    put the 5.5 hours after 18:30 UTC into the wrong day. `now` defaults to the
    current instant; passing one lets a test pin the boundary.
    """
    now = now or datetime.now(IST)
    return now.astimezone(IST).date()


def summary_text(count: int, spent: Decimal, received: Decimal) -> str:
    """The message body: the day's entry count and totals.

    `spent` is always shown (the point of the daily summary); `received` only
    when there was income, so an ordinary day reads clean. A day with no entries
    still sends — the summary is unconditional (§12).
    """
    if count == 0:
        return "🌙 No entries logged today."
    entries = "entry" if count == 1 else "entries"
    line = f"🌙 Today: {count} {entries}, spent {format_amount(spent)}"
    if received > 0:
        line += f", received {format_amount(received)}"
    return line + "."


def run(conn) -> int:
    """Send each user their day summary; return how many were sent."""
    day = today_ist()
    users = all_users(conn)
    for user_id, telegram_user_id in users:
        count, spent, received = day_summary(conn, user_id, day)
        send_message(telegram_user_id, summary_text(count, spent, received))
        log_reminder(conn, user_id, "evening")
    return len(users)


def main() -> None:
    """Cron entry point: `python -m kanakko.jobs.evening`."""
    configure_logging()
    with connect() as conn:
        sent = run(conn)
    log.info("evening summary sent to %d user(s)", sent)


if __name__ == "__main__":
    main()
