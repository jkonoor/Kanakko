"""Refunds: negative spending, linked to what they refund (§18).

A refund is not income — it reduces the category total of the expense it
refunds. `create_refund` is the write side; migration 014's trigger is what
actually stops the sum of refunds against a transaction from exceeding it —
this function does not re-check that in Python, so a race between two
concurrent refunds still lands on the correct, DB-enforced answer rather than
a check-then-write gap.
"""

from datetime import date
from decimal import Decimal

import psycopg

from kanakko.db.audit import _record_event


def create_refund(
    conn: psycopg.Connection,
    user_id: int,
    original_txn_id: int,
    amount: Decimal,
    occurred_on: date,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Refund `amount` of `original_txn_id`, scoped to `user_id`'s household (§16, §18).

    Household-scoped, not user-scoped: §16's shared ledger means any member may
    refund any member's expense, the same reach `recent_transactions` and the
    monthly summary already have — unlike `edits.py`'s per-row mutations, which
    §16 restricts to "the member who entered it". Only a live `expense` row is
    refundable (a `None` return for income/transfer/already-deleted/foreign-
    household), which is what keeps "reduces the category total, never income"
    true by construction rather than by a report-side special case.

    `account_id` and `category` are copied from the original row onto the new
    refund row: the money physically returns to the same account, and
    denormalizing the category (rather than joining back to the original in
    every report query) is what lets `reports.py` net a refund against its
    category with a plain `GROUP BY category`. Raises whatever
    `psycopg.errors.RaiseException` migration 014's trigger raises if this
    refund, combined with any already recorded against `original_txn_id`,
    would exceed the original amount — the caller does not need to re-check
    that here. Returns the stored row, or `None` when there is no matching
    live expense. Does not commit — the caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT account_id, category FROM active_transactions"
            " WHERE txn_id = %s AND type = 'expense'"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)",
            (original_txn_id, user_id),
        )
        original = cur.fetchone()
        if original is None:
            return None
        account_id, category = original

        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, account_id, amount, type, category, occurred_on, refund_of_txn_id)"
            " VALUES (%s,"
            "  (SELECT household_id FROM household_members WHERE user_id = %s),"
            "  %s, %s, 'refund', %s, %s, %s)"
            " RETURNING txn_id",
            (user_id, user_id, account_id, amount, category, occurred_on, original_txn_id),
        )
        (txn_id,) = cur.fetchone()
        stored = {
            "amount": amount,
            "category": category,
            "occurred_on": occurred_on,
            "refund_of_txn_id": original_txn_id,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="refund",
                      before=None, after=stored, source=source, update_id=update_id)
    return {"txn_id": txn_id, **stored}
