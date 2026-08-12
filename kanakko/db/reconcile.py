"""Reconciliation adjustments: the ledger meets the real world (§18).

Nothing is connected to a bank, so the ledger drifts. §18's fix is a nudge
("what does your bank say?") plus, when the answer disagrees, a **visible**
adjustment row against the `external` account — never a silent rewrite of a
balance. `create_adjustment` is that write; the weekly nudge and the reply that
supplies `reported_balance` are a separate, UX-facing task on top of this one.
"""

from datetime import date
from decimal import Decimal

import psycopg

from kanakko.db.audit import _record_event


def create_adjustment(
    conn: psycopg.Connection,
    user_id: int,
    account_id: int,
    reported_balance: Decimal,
    occurred_on: date,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Reconcile `account_id` against `reported_balance`, scoped to `user_id`'s household (§16, §18).

    Recomputes the account's derived balance the same way `accounts.account_balances`
    does (§18: never a stored total) — including the credit sign convention, so
    "what does your bank say" and "what does your card statement say you owe" are
    both just `reported_balance` compared to the same presented figure. When the two
    already agree, nothing is written and this returns `None`: an adjustment for zero
    drift would be a ledger row that says nothing.

    A disagreement becomes an ordinary `transfer` between `account_id` and the
    household's `external` account (011's existing type, no new schema) sized so the
    *presented* balance moves by exactly the drift — worked out algebraically rather
    than branched by kind: writing `delta_raw = sign * drift` (`sign` is −1 for
    `credit`, else +1, the same negation `account_balances` applies) makes the
    transfer amount `abs(delta_raw)` regardless of kind, and its direction the sign
    of `delta_raw` — external → account when positive, account → external otherwise.
    For a `credit` account this means "owe more than the ledger thought" moves money
    *out* of the card account, the same direction an ordinary swipe (an expense on
    that account) already moves it, so the adjustment composes with the existing
    balance formula instead of needing a special case there.

    `account_id` must name a live, non-`external` account in `user_id`'s own
    household (§16) — `external` is structural bookkeeping, never something a
    reconcile nudge is sent for, and a foreign or unknown id returns `None` the same
    way. The write and its audit row (`action="adjustment"`, distinct from a
    user-typed `confirm`, so the audit query can tell a system correction from an
    ordinary transfer) share one `conn.transaction()` (§17). Returns the stored row,
    or `None` when there is nothing to reconcile against or nothing to write. Does
    not commit — the caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT a.kind,"
            "  (CASE WHEN a.kind = 'credit' THEN -1 ELSE 1 END) * ("
            "     a.opening_balance"
            "     + coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'income'   AND account_id = a.account_id), 0)"
            "     + coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'refund'   AND account_id = a.account_id), 0)"
            "     - coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'expense'  AND account_id = a.account_id), 0)"
            "     + coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'transfer' AND to_account_id   = a.account_id), 0)"
            "     - coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'transfer' AND from_account_id = a.account_id), 0)"
            "  ) AS presented_balance,"
            "  (SELECT account_id FROM accounts"
            "    WHERE household_id = a.household_id AND kind = 'external' AND deleted_at IS NULL)"
            " FROM accounts a"
            " WHERE a.account_id = %s"
            "   AND a.household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            "   AND a.kind <> 'external' AND a.deleted_at IS NULL",
            (account_id, user_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        kind, presented_balance, external_account_id = row
        if external_account_id is None:
            return None
        drift = reported_balance - presented_balance
        if drift == 0:
            return None

        sign = -1 if kind == "credit" else 1
        delta_raw = sign * drift
        amount = abs(delta_raw)
        if delta_raw > 0:
            from_account_id, to_account_id = external_account_id, account_id
        else:
            from_account_id, to_account_id = account_id, external_account_id

        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, type, amount, note, occurred_on,"
            "  from_account_id, to_account_id)"
            " VALUES (%s,"
            "  (SELECT household_id FROM household_members WHERE user_id = %s),"
            "  'transfer', %s, 'Reconciliation adjustment', %s, %s, %s)"
            " RETURNING txn_id",
            (user_id, user_id, amount, occurred_on, from_account_id, to_account_id),
        )
        (txn_id,) = cur.fetchone()
        stored = {
            "amount": amount,
            "occurred_on": occurred_on,
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="adjustment",
                      before=None, after=stored, source=source, update_id=update_id)
    return {"txn_id": txn_id, **stored}
