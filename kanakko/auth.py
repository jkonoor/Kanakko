"""Authorization: is this Telegram user permitted at all? (DECISIONS §16).

Telegram is the identity provider (§13); what was missing is the one check before
any work — before the LLM call that costs money — that decides whether an update
is served. This module owns that policy. The invite lookups it will grow live as
plain SQL in `db.py`; the config that governs them lives here.
"""

import os

import psycopg

from kanakko.db import user_exists


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
