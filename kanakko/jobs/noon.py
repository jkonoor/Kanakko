"""Noon nudge — 12:00 daily, suppressed when the day already has activity (§12).

The nudge only arrives when it's right: if the user has logged anything since the
previous evening summary, they're already engaged and a "did you log anything?"
would just be noise, so it's skipped. That "since the previous evening summary"
boundary is the actual `sent_at` of the user's last evening summary, read from
`reminder_log` (`last_reminder_at`). Only when a user has no logged summary yet
does it fall back to the nominal most-recent 21:00 `Asia/Kolkata`
(`previous_evening_ist`) — deterministic because the evening summary fires at a
fixed 21:00 daily (§12), so at the noon run it's yesterday's 21:00.

The `cron` service runs `python -m kanakko.jobs.noon` at 12:00 `Asia/Kolkata`;
the crontab belongs to a later task.
"""

import logging
from datetime import datetime, timedelta

from kanakko import configure_logging
from kanakko.db import all_users, connect, last_reminder_at, log_reminder, logged_since
from kanakko.jobs import fan_out
from kanakko.jobs.evening import IST
from kanakko.tg import send_message

log = logging.getLogger(__name__)

EVENING_HOUR = 21

NUDGE_TEXT = "👋 Logged anything today? Send it over and I'll track it."


def previous_evening_ist(now: datetime | None = None) -> datetime:
    """The most recent 21:00 `Asia/Kolkata` strictly before `now` (§10, §12).

    The suppression window opens at the previous evening summary. Because that
    summary fires at a fixed 21:00 IST daily, the window start is deterministic:
    today's 21:00 if we're already past it, else yesterday's. At the noon run it
    is always yesterday's 21:00. Computing this in UTC would slide the boundary by
    5.5 hours and suppress (or nudge) the wrong users, so it is pinned to IST.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    evening = now.replace(hour=EVENING_HOUR, minute=0, second=0, microsecond=0)
    if evening > now:
        evening -= timedelta(days=1)
    return evening


def run(conn) -> int:
    """Nudge each user with no activity since the last evening summary; return the count.

    The suppression boundary is per user: the actual instant of that user's last
    evening summary from `reminder_log` (§12), falling back to the nominal 21:00
    IST (`previous_evening_ist`) only for a user with no logged summary yet — a
    brand-new user, or before the evening job's first run. Suppressed users don't
    count toward the return — it's the number actually sent.
    """
    fallback = previous_evening_ist()

    def deliver(user_id: int, telegram_user_id: int) -> bool:
        since = last_reminder_at(conn, user_id, "evening") or fallback
        if logged_since(conn, user_id, since):
            return False  # suppressed, not failed — §12
        send_message(telegram_user_id, NUDGE_TEXT)
        log_reminder(conn, user_id, "noon")
        return True

    return fan_out(conn, all_users(conn), deliver)


def main() -> None:
    """Cron entry point: `python -m kanakko.jobs.noon`."""
    configure_logging()
    with connect() as conn:
        sent = run(conn)
    log.info("noon nudge sent to %d user(s)", sent)


if __name__ == "__main__":
    main()
