"""Authorization: is this Telegram user permitted at all? (DECISIONS §16).

Telegram is the identity provider (§13); what was missing is the one check before
any work — before the LLM call that costs money — that decides whether an update
is served. This module owns that policy. The invite lookups it will grow live as
plain SQL in `db.py`; the config that governs them lives here.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from kanakko.db import count_updates_on_day, user_exists

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_DAILY_CAP = 50


def signup_mode() -> str:
    """`'invite'` (closed, the default) or `'open'`, from $SIGNUP_MODE (§16).

    Fails closed like §15's secret: only the exact string `open` opens signup;
    unset, empty, or any typo (`Open`, `yes`, `true`) stays `invite`. Going from
    a closed beta to public signup is one deliberate env change, never an accident
    of casing or whitespace.
    """
    return "open" if os.environ.get("SIGNUP_MODE") == "open" else "invite"


def is_authorized(conn: psycopg.Connection, telegram_user_id: int) -> bool:
    """May this Telegram user be served at all? — the one check before any work (§16).

    Authorized when signup is `open` (anyone may use the bot) or when the user
    already has a `users` row (they were admitted earlier — pre-existing, or via an
    invite once onboarding lands). In `invite` mode an unrecognised user is refused
    and nothing is stored, not even a user row — which is why this asks
    `user_exists`, never `get_or_create_user`.
    """
    return signup_mode() == "open" or user_exists(conn, telegram_user_id)


def daily_message_cap() -> int:
    """Max handled messages per user per IST day, from $DAILY_MESSAGE_CAP (§16).

    Never hardcoded: the operator raises or lowers the cost cap with one env
    change (§16), the way `OPENROUTER_MODEL` and `SIGNUP_MODE` are config. Read
    with `or` (the §2 compose trap: `:-` leaves the var present-but-empty), and an
    unset or non-integer value falls back to the §16 default of 50 rather than
    crashing the webhook — a fat-fingered env must never take the bot down.
    """
    raw = os.environ.get("DAILY_MESSAGE_CAP") or ""
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DAILY_CAP


def within_daily_cap(
    conn: psycopg.Connection, user_id: int, *, now: datetime | None = None
) -> bool:
    """True while `user_id` is still under today's message cap (§16 cost control).

    Checked *before* the LLM call (§2 costs money): counts the user's already-
    handled updates on the current IST day (§10) and admits them while that count
    is below `daily_message_cap()`. The current update is not yet claimed when this
    runs, so a cap of 50 admits 50 messages and refuses the 51st. `now` defaults to
    the current instant; a test pins it to prove the IST-midnight rollover.
    """
    ist_day = (now or datetime.now(IST)).astimezone(IST).date()
    return count_updates_on_day(conn, user_id, ist_day) < daily_message_cap()
