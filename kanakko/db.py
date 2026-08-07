"""Database access for the confirm flow (DECISIONS §1, §6, §7, §9).

Plain SQL via psycopg, no ORM (§7). A parsed transaction lives as a
`pending_transactions` row from the moment its confirm card is sent until the
user taps Confirm or Cancel: `save_pending` writes that row, `confirm_pending`
turns it into a real `transactions` row and clears the pending one.

Amounts are serialized as strings and read back through the `Transaction`
model, whose validator routes them through `money.parse_amount` (§9) — a JSON
number (a float) would be refused there, so a float can never reach the ledger.
Reads elsewhere go through `active_transactions` (§6).
"""

import json
import os
from datetime import date, datetime
from decimal import Decimal
from functools import partial

import psycopg
from psycopg.types.json import Jsonb

from kanakko.parse import Transaction

# `default=str` renders the `Decimal` amount and the `date` a ledger row carries;
# JSONB stores them as their canonical strings ("12.50", "2026-08-05"), never a
# float (§9).
_json_dumps = partial(json.dumps, default=str)


def _record_event(
    cur: psycopg.Cursor,
    *,
    txn_id: int,
    user_id: int,
    action: str,
    before: dict | None,
    after: dict | None,
    source: str,
    update_id: int | None,
) -> None:
    """Write one `transaction_events` audit row (§17).

    Called on the *same cursor*, inside each money function's own
    `conn.transaction()` block, so the audit row commits and rolls back with the
    money statement — physical adjacency is the only form of this that cannot come
    apart (§17). `before`/`after` are the row's fields; the side that doesn't
    exist for an action is `None` (a confirm has no before, an undo/delete no
    after).
    """
    cur.execute(
        "INSERT INTO transaction_events"
        " (txn_id, user_id, action, before, after, source, update_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            txn_id,
            user_id,
            action,
            Jsonb(before, dumps=_json_dumps) if before is not None else None,
            Jsonb(after, dumps=_json_dumps) if after is not None else None,
            source,
            update_id,
        ),
    )


def connect() -> psycopg.Connection:
    """Open a connection from `DATABASE_URL`. Fails closed if it is unset."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(dsn)


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


def save_pending(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, txn: Transaction
) -> int:
    """Store `txn` as a pending row keyed by the confirm card's message id.

    `model_dump(mode="json")` serializes `amount` as a string and `date` as ISO,
    so nothing float-shaped is persisted; `confirm_pending` reverses it through
    the same model. Returns the new `pending_id`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_transactions (user_id, telegram_message_id, parsed)"
            " VALUES (%s, %s, %s) RETURNING pending_id",
            (user_id, telegram_message_id, Jsonb(txn.model_dump(mode="json"))),
        )
        (pending_id,) = cur.fetchone()
    return pending_id


def confirm_pending(
    conn: psycopg.Connection,
    user_id: int,
    telegram_message_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Write user's pending row for `telegram_message_id` to `transactions`, clear it.

    Scoped by `user_id` because Telegram message ids repeat per chat, not
    globally (§1): keying on the message id alone would let one user's Confirm
    write another user's pending transaction. `save_pending` stores `user_id`;
    this reverses it with the same scoping.

    The read → insert → delete → audit run in one transaction so a crash can never
    store a transaction while leaving its pending row live (a later double
    confirm), nor clear the pending row with nothing stored, nor write the ledger
    row without its audit trail (§17). `source`/`update_id` are the acting update's
    correlation id, taken as arguments so the audit write cannot come apart from
    the money write (§17). Returns the stored row — `txn_id` plus its fields, so
    the caller can log the amount (§17) — or `None` when there is no pending row
    (Telegram redelivers taps it already got a 200 for, so confirming twice must
    not write the transaction twice). Does not commit — the caller owns the
    transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed = row
        txn = Transaction.model_validate(parsed)
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, amount, type, category, note, occurred_on)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING txn_id",
            (user_id, txn.amount, txn.type, txn.category, txn.note, txn.date),
        )
        (txn_id,) = cur.fetchone()
        cur.execute("DELETE FROM pending_transactions WHERE pending_id = %s", (pending_id,))
        stored = {
            "amount": txn.amount,
            "type": txn.type,
            "category": txn.category,
            "note": txn.note,
            "occurred_on": txn.date,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="confirm",
                      before=None, after=stored, source=source, update_id=update_id)
    return {"txn_id": txn_id, **stored}


def set_pending_category(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, category: str
) -> Transaction | None:
    """Set `category` on the user's pending row for `telegram_message_id` (§5).

    The correction path for the most-often-wrong field: a `cat:<name>` tap
    re-writes the pending row's category and returns the updated `Transaction`
    so the handler can re-render the card. Scoped by `user_id` like the confirm/
    cancel reads — message ids repeat per chat (§1). Round-trips through the
    `Transaction` model, so `category` is re-validated against the closed set
    (§11) and the amount stays a string on the way back to JSONB (§9). Returns
    `None` when there is no pending row (a stale card). Does not commit — the
    caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed = row
        txn = Transaction.model_validate({**parsed, "category": category})
        cur.execute(
            "UPDATE pending_transactions SET parsed = %s WHERE pending_id = %s",
            (Jsonb(txn.model_dump(mode="json")), pending_id),
        )
    return txn


def undo_last(
    conn: psycopg.Connection,
    user_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Soft-delete the user's most recent confirmed transaction, return its fields (§5, §6).

    `/undo` corrects the last entry: it sets `deleted_at` on the newest live row
    rather than hard-deleting it, so the ledger stays recoverable (§6). The row is
    chosen from `active_transactions` — the view that already hides soft-deleted
    rows — so a *second* `/undo` walks back to the previous entry instead of
    re-deleting the one just removed (reading `transactions` directly would keep
    latching onto the already-deleted newest row). Scoped by `user_id` (§1).
    The soft-delete and its audit row (§17) share one `conn.transaction()`, so a
    crash between them is impossible; `source`/`update_id` are the correlation id
    of the update doing the undo. Returns the removed row's fields — including its
    `txn_id` (§17) — for the confirmation reply and the event log, or `None` when
    there is no live transaction to undo. Does not commit — the caller owns the
    transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE transactions SET deleted_at = now()"
            " WHERE txn_id = ("
            "   SELECT txn_id FROM active_transactions"
            "   WHERE user_id = %s ORDER BY created_at DESC, txn_id DESC LIMIT 1"
            " )"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        txn_id, amount, type_, category, note, occurred_on = row
        removed = {
            "amount": amount,
            "type": type_,
            "category": category,
            "note": note,
            "occurred_on": occurred_on,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="undo",
                      before=removed, after=None, source=source, update_id=update_id)
    return {"txn_id": txn_id, **removed}


def claim_update(conn: psycopg.Connection, update_id: int, user_id: int) -> bool:
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
    cap off this same table.
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

    The metering read behind the per-user daily message cap: every inbound message
    is one LLM call (§2), so an unbounded user is an unbounded bill. Counts
    `processed_updates` rows the user claimed, bucketed on `processed_at`
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


def day_summary(
    conn: psycopg.Connection, user_id: int, day: date
) -> tuple[int, Decimal, Decimal]:
    """`(entry_count, spent, received)` for `user_id`'s live rows on `day` (§6, §9, §12).

    Buckets on `occurred_on` — when the money moved, not when it was logged — and
    reads `active_transactions` so a soft-deleted row never re-enters a total. The
    two sums come back as `NUMERIC` → `Decimal` (never float, §9); an empty day
    yields `(0, 0.00, 0.00)`. `day` is a plain date the caller computes in
    `Asia/Kolkata` (§10), keeping the timezone boundary in one testable place.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*),"
            " coalesce(sum(amount) FILTER (WHERE type = 'expense'), 0),"
            " coalesce(sum(amount) FILTER (WHERE type = 'income'), 0)"
            " FROM active_transactions WHERE user_id = %s AND occurred_on = %s",
            (user_id, day),
        )
        count, spent, received = cur.fetchone()
    return count, spent, received


def month_summary(
    conn: psycopg.Connection, user_id: int, first_day: date, next_first_day: date
) -> tuple[Decimal, Decimal, list[tuple[str, Decimal]]]:
    """`(income, expenses, top_categories)` for `user_id`'s live rows in the month (§6, §9, §12).

    The range is half-open `[first_day, next_first_day)` on `occurred_on` — when
    the money moved — so a 23:50 IST entry on the month's last day lands in that
    month (the caller computes both boundaries in `Asia/Kolkata`, §10). Reads
    `active_transactions`, so a soft-deleted row never re-enters the totals (§6).
    The two sums come back as `NUMERIC` → `Decimal` (never float, §9). Top
    categories are the month's expense categories with their totals, biggest
    first — the caller decides how many to show.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT"
            " coalesce(sum(amount) FILTER (WHERE type = 'income'), 0),"
            " coalesce(sum(amount) FILTER (WHERE type = 'expense'), 0)"
            " FROM active_transactions"
            " WHERE user_id = %s AND occurred_on >= %s AND occurred_on < %s",
            (user_id, first_day, next_first_day),
        )
        income, expenses = cur.fetchone()
        cur.execute(
            "SELECT category, sum(amount) FROM active_transactions"
            " WHERE user_id = %s AND occurred_on >= %s AND occurred_on < %s"
            " AND type = 'expense'"
            " GROUP BY category ORDER BY sum(amount) DESC, category",
            (user_id, first_day, next_first_day),
        )
        top = cur.fetchall()
    return income, expenses, top


def recent_transactions(
    conn: psycopg.Connection, user_id: int, limit: int = 10
) -> list[tuple]:
    """The user's most recent live transactions, newest first (§6, §13).

    The dashboard's recent list: each row carries its `txn_id` so the per-row
    delete button can name it. Reads `active_transactions`, so a soft-deleted row
    never reappears (§6). `limit` caps the list — the dashboard shows a handful,
    not the whole ledger. Amounts come back as `NUMERIC` → `Decimal` (§9). Ordered
    by `created_at` (when logged) so the list matches the order entries were added.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT txn_id, amount, type, category, note, occurred_on"
            " FROM active_transactions WHERE user_id = %s"
            " ORDER BY created_at DESC, txn_id DESC LIMIT %s",
            (user_id, limit),
        )
        return cur.fetchall()


def soft_delete_transaction(
    conn: psycopg.Connection,
    user_id: int,
    txn_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Soft-delete one live transaction by id, scoped to `user_id` (§6, §13).

    The dashboard's per-row delete: sets `deleted_at` on the row so the ledger
    stays recoverable (§6), scoped to `user_id` so one user cannot delete
    another's row by guessing an id (§1). The row is chosen from
    `active_transactions`, so deleting an already-deleted (or another user's) row
    is a no-op returning `None`, not a second write — the subquery yields no
    `txn_id`, and `WHERE txn_id = NULL` matches nothing. Mirrors `undo_last`'s
    read-through-the-view pattern. The delete and its audit row (§17) share one
    `conn.transaction()`; `source`/`update_id` are the correlation id (a Mini App
    delete carries `source="miniapp"` and no `update_id`, §17 gap 2). Returns the
    deleted row — `txn_id` plus its amount so the caller can log it (§17) — or
    `None`. Does not commit — the caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE transactions SET deleted_at = now()"
            " WHERE txn_id = ("
            "   SELECT txn_id FROM active_transactions"
            "   WHERE user_id = %s AND txn_id = %s"
            " )"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id, txn_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        tid, amount, type_, category, note, occurred_on = row
        before = {
            "amount": amount,
            "type": type_,
            "category": category,
            "note": note,
            "occurred_on": occurred_on,
        }
        _record_event(cur, txn_id=tid, user_id=user_id, action="delete",
                      before=before, after=None, source=source, update_id=update_id)
    return {"txn_id": tid, "amount": amount}


def set_transaction_category(
    conn: psycopg.Connection,
    user_id: int,
    txn_id: int,
    category: str,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Set the category of one live transaction, scoped to `user_id` (§5, §13).

    The dashboard's per-row category change (task 101): corrects the most-often-
    wrong field on an already-confirmed entry. Scoped to `user_id` so one user
    cannot relabel another's row by guessing an id (§1); the row is chosen from
    `active_transactions`, so a deleted or foreign id yields no `txn_id` and the
    UPDATE matches nothing, returning `None` rather than a stray write. The caller
    validates `category` against the closed set (`categories.py`) before this runs.
    Mirrors `soft_delete_transaction`.

    Category change history is the audit data §17 names as genuinely missing, so
    the old category is read with a `SELECT` before the `UPDATE`, in the same
    transaction — Postgres 16 here has no `RETURNING OLD.*` form — and both flank
    the audit row's `before`/`after`. `source`/`update_id` are the correlation id.
    Returns the updated row — `txn_id` plus its amount so the caller can log it
    (§17) — or `None`. Does not commit — the caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT category FROM active_transactions"
            " WHERE user_id = %s AND txn_id = %s",
            (user_id, txn_id),
        )
        old = cur.fetchone()
        if old is None:
            return None
        (old_category,) = old
        cur.execute(
            "UPDATE transactions SET category = %s"
            " WHERE txn_id = ("
            "   SELECT txn_id FROM active_transactions"
            "   WHERE user_id = %s AND txn_id = %s"
            " )"
            " RETURNING txn_id, amount",
            (category, user_id, txn_id),
        )
        tid, amount = cur.fetchone()
        _record_event(cur, txn_id=tid, user_id=user_id, action="recategorise",
                      before={"category": old_category}, after={"category": category},
                      source=source, update_id=update_id)
    return {"txn_id": tid, "amount": amount}


def logged_since(conn: psycopg.Connection, user_id: int, since: datetime) -> bool:
    """True if `user_id` has any live transaction logged since `since` (§6, §12).

    The noon nudge's suppression check: was the user already active since the
    previous evening summary? `created_at` — when the row was *logged*, not
    `occurred_on` — is the right column, so recording a back-dated expense this
    morning still counts as activity. Reads `active_transactions`, so a row the
    user logged and then undid doesn't keep the nudge suppressed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM active_transactions"
            " WHERE user_id = %s AND created_at >= %s LIMIT 1",
            (user_id, since),
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


def cancel_pending(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int
) -> int | None:
    """Discard the user's pending row for `telegram_message_id`, storing nothing (§5).

    Scoped by `user_id` for the same reason as `confirm_pending` — message ids
    repeat per chat, so a Cancel keyed on the id alone could delete another
    user's pending card. Returns the deleted `pending_id`, or `None` when there
    is nothing to cancel (a redelivered tap Telegram already got a 200 for), so
    the handler can tell a fresh cancel from a repeat. Does not commit — the
    caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s RETURNING pending_id",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
    return row[0] if row else None
