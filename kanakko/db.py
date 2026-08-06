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

import os

import psycopg
from psycopg.types.json import Jsonb

from kanakko.parse import Transaction


def connect() -> psycopg.Connection:
    """Open a connection from `DATABASE_URL`. Fails closed if it is unset."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(dsn)


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


def confirm_pending(conn: psycopg.Connection, telegram_message_id: int) -> int | None:
    """Write the pending row for `telegram_message_id` to `transactions`, clear it.

    The read → insert → delete run in one transaction so a crash can never store
    a transaction while leaving its pending row live (a later double confirm), nor
    clear the pending row with nothing stored. Returns the new `txn_id`, or `None`
    when there is no pending row — Telegram redelivers taps it already got a 200
    for, so confirming twice must not write the transaction twice.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, user_id, parsed FROM pending_transactions"
            " WHERE telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (telegram_message_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, user_id, parsed = row
        txn = Transaction.model_validate(parsed)
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, amount, type, category, note, occurred_on)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING txn_id",
            (user_id, txn.amount, txn.type, txn.category, txn.note, txn.date),
        )
        (txn_id,) = cur.fetchone()
        cur.execute("DELETE FROM pending_transactions WHERE pending_id = %s", (pending_id,))
    return txn_id
