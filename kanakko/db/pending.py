"""The confirm flow: pending rows, confirm, cancel, category correction, undo.

A parsed transaction lives as a `pending_transactions` row from the moment its
confirm card is sent until the user taps Confirm or Cancel: `save_pending` writes
that row, `confirm_pending` turns it into a real `transactions` row and clears
the pending one, `cancel_pending` discards it, `undo_last` walks back the newest
confirmed entry.

Amounts are serialized as strings and read back through the `Transaction` model,
whose validator routes them through `money.parse_amount` (§9) — a JSON number (a
float) would be refused there, so a float can never reach the ledger.
"""

import psycopg
from psycopg.types.json import Jsonb

from kanakko.db.audit import _record_event
from kanakko.parse import Transaction


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

    The row is homed in the entering user's household (§16): `household_id` is the
    `household_members` row for `user_id`, the tenancy axis §16 moves off the user.
    A user with no membership yields NULL and the NOT NULL constraint (migration
    008) refuses the insert rather than orphaning money from every household total.
    `account_id` is the household's default account (§18) — a transaction that
    names no account lands there; a household with no default (a test fixture that
    bypassed onboarding) yields NULL, which `accounts.account_id` still permits
    until the NOT NULL write-wiring task lands.

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
            "SELECT hm.household_id, a.account_id FROM household_members hm"
            " LEFT JOIN accounts a"
            "   ON a.household_id = hm.household_id AND a.is_default AND a.deleted_at IS NULL"
            " WHERE hm.user_id = %s",
            (user_id,),
        )
        home = cur.fetchone()
        household_id, account_id = home if home is not None else (None, None)
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, account_id, amount, type, category, note, occurred_on)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING txn_id",
            (user_id, household_id, account_id,
             txn.amount, txn.type, txn.category, txn.note, txn.date),
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
    latching onto the already-deleted newest row). Scoped by `user_id` — undo
    removes *your own* last entry, never a housemate's (§16) — and additionally by
    the household the user belongs to, so no read of the view ever spans households
    (§1, §16).
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
            "   WHERE user_id = %s"
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            "   ORDER BY created_at DESC, txn_id DESC LIMIT 1"
            " )"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id, user_id),
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
