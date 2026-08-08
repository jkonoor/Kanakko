"""The scheduled jobs' reads and bookkeeping (§12): noon suppression, reminder log."""

from datetime import datetime

import psycopg


def logged_since(conn: psycopg.Connection, user_id: int, since: datetime) -> bool:
    """True if `user_id` has any live transaction logged since `since` (§6, §12).

    The noon nudge's suppression check: was the user already active since the
    previous evening summary? `created_at` — when the row was *logged*, not
    `occurred_on` — is the right column, so recording a back-dated expense this
    morning still counts as activity. Reads `active_transactions`, so a row the
    user logged and then undid doesn't keep the nudge suppressed. Scoped by
    `user_id` — the nudge is suppressed *per person*, so a member who logged
    nothing is still nudged even if a housemate was active (§16) — and additionally
    by the user's household, so no read of the view ever spans households (§16).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM active_transactions"
            " WHERE user_id = %s"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND created_at >= %s LIMIT 1",
            (user_id, user_id, since),
        )
        return cur.fetchone() is not None


def log_reminder(conn: psycopg.Connection, user_id: int, kind: str) -> None:
    """Record that a `kind` reminder ('noon' | 'evening' | 'monthly') was sent now (§12).

    Every scheduled job writes one of these per message it sends; the noon nudge
    reads them (`last_reminder_at`) to find the *actual* last evening summary
    instant rather than assuming a fixed 21:00. `kind` is checked against the
    table's CHECK constraint, so a typo is a hard error, not a silent no-op. Does
    not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reminder_log (user_id, kind) VALUES (%s, %s)",
            (user_id, kind),
        )


def last_reminder_at(
    conn: psycopg.Connection, user_id: int, kind: str
) -> datetime | None:
    """When `user_id` was last sent a `kind` reminder, or `None` if never (§12).

    The noon nudge's suppression boundary: the actual instant of the previous
    evening summary. Reading it here means a missed or delayed summary shifts the
    window to when the summary really went out, not the nominal 21:00 — the caller
    falls back to that nominal boundary only when there is no logged summary yet.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sent_at FROM reminder_log"
            " WHERE user_id = %s AND kind = %s"
            " ORDER BY sent_at DESC LIMIT 1",
            (user_id, kind),
        )
        row = cur.fetchone()
    return row[0] if row else None
