"""Users and update metering (§1, §14, §16).

The internal `users.user_id` every table keys on, plus the `processed_updates`
bookkeeping behind Telegram idempotency (§14) and the §16 daily cost cap.
"""

from datetime import date

import psycopg


def get_or_create_user(conn: psycopg.Connection, telegram_user_id: int) -> int:
    """Resolve `telegram_user_id` to its internal `users.user_id`, creating it once.

    Every table keys on the internal `user_id` (§1), so the message handler turns
    the Telegram id it sees into that id here. `ON CONFLICT DO NOTHING` makes a
    second message from the same user a no-op insert rather than a unique
    violation; the `UNION ALL … LIMIT 1` then returns the existing row on that
    path. Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH ins AS ("
            "  INSERT INTO users (telegram_user_id) VALUES (%s)"
            "  ON CONFLICT (telegram_user_id) DO NOTHING RETURNING user_id"
            ")"
            " SELECT user_id FROM ins"
            " UNION ALL"
            " SELECT user_id FROM users WHERE telegram_user_id = %s"
            " LIMIT 1",
            (telegram_user_id, telegram_user_id),
        )
        (user_id,) = cur.fetchone()
    return user_id


def user_exists(conn: psycopg.Connection, telegram_user_id: int) -> bool:
    """True if a `users` row already exists for this Telegram id — a pure lookup
    that creates nothing, unlike `get_or_create_user`.

    The §16 authorization gate uses it to refuse an unrecognised user *before* any
    LLM call, without minting the very row §16 says must not be stored for a
    refused update.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = %s", (telegram_user_id,)
        )
        return cur.fetchone() is not None


def claim_update(
    conn: psycopg.Connection, update_id: int, user_id: int | None
) -> bool:
    """Record `update_id` as processed by `user_id`; True the first time, False on a repeat (§14).

    Telegram redelivers any update it did not answer 2xx for, so the webhook calls
    this before running a handler and skips the handler when it returns False —
    the one place that makes every handler idempotent against redelivery. It
    matters most for `/undo`, which soft-deletes "the newest live row" with no
    per-message anchor (Confirm/Cancel key on the card's message id): a redelivery
    would otherwise soft-delete a *second* real transaction and drop it from every
    total. The INSERT runs in the caller's transaction, so the claim commits with
    the handler's writes and rolls back with them — a handler that 500s is
    retried. Does not commit — the caller owns the transaction.

    `user_id` stamps the row so `count_updates_on_day` can meter the §16 daily cost
    cap off this same table — but only for the metered (text-parse) path; the caller
    passes `None` for the free Confirm/Cancel/category/undo claims so their rows stay
    unmetered and the cap counts exactly the messages that cost an LLM call.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_updates (update_id, user_id) VALUES (%s, %s)"
            " ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
            (update_id, user_id),
        )
        return cur.fetchone() is not None


def count_updates_on_day(
    conn: psycopg.Connection, user_id: int, ist_day: date
) -> int:
    """How many updates this user has had handled on `ist_day` (§16 cost cap).

    The metering read behind the per-user daily message cap: each text-parse
    message is one LLM call (§2), so an unbounded user is an unbounded bill. Counts
    only the `processed_updates` rows stamped with this `user_id` — `claim_update`
    stamps it on the parse path alone, leaving the free Confirm/Cancel/category/undo
    claims NULL, so the `WHERE user_id = %s` counts exactly the LLM calls, not the
    free taps. Bucketed on `processed_at`
    **`AT TIME ZONE 'Asia/Kolkata'`** so the day rolls over at IST midnight, not
    UTC (§10) — a message at 23:50 IST counts against that IST day, not the next
    one it falls into in UTC. `ist_day` is the current IST date the caller
    computes, keeping the one clock read in the webhook. Reads the base table, not
    `active_transactions` — this counts updates, not ledger rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM processed_updates"
            " WHERE user_id = %s"
            " AND (processed_at AT TIME ZONE 'Asia/Kolkata')::date = %s",
            (user_id, ist_day),
        )
        (n,) = cur.fetchone()
    return n


def all_users(conn: psycopg.Connection) -> list[tuple[int, int]]:
    """Every user as `(user_id, telegram_user_id)` — the scheduled jobs' fan-out.

    The jobs read a user's ledger by internal `user_id` (§1) but send to the
    Telegram id; a private chat's `chat_id` is that Telegram id. Not a ledger
    read, so it goes straight to `users`.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, telegram_user_id FROM users ORDER BY user_id")
        return cur.fetchall()
