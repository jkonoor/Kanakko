"""Derived account balances (DECISIONS §18).

§18: "Balance is derived, never stored: `opening_balance + inflows − outflows`,
computed from the ledger. A stored running total is a second source of truth that
drifts silently, which is the one failure mode a money app cannot survive." So
there is no balance column — this recomputes it from `active_transactions` on
every read (§6: a soft-deleted row must never re-enter a total, balances included).

The sign convention is part of the spec, not a comment: a `credit` account "reads
as what is owed" (§18), so its asset balance — negative while in debt — is negated
on the way out. A `spending`/`locked`/`external` account reads as the money it
holds; a `credit` account reads as what you owe (positive while in debt).
"""

import psycopg


def account_balances(conn: psycopg.Connection, user_id: int) -> list[dict]:
    """Derived balance of every live account in `user_id`'s household (§6, §16, §18).

    Balance is `opening_balance + inflows − outflows`, computed from the ledger and
    never stored (§18). Inflows are income landing in the account and transfers into
    it; outflows are its expenses and transfers out of it. A `transfer` row touches
    two accounts and never a total, so it is summed by its endpoint columns, not by
    `account_id`. Reads `active_transactions`, so a soft-deleted row never counts
    (§6). Household-scoped: the outer predicate confines the accounts to `user_id`'s
    household (§16), and each per-account sum keys on `account_id` — an account
    belongs to exactly one household, so the sum can never span households. Amounts
    are `NUMERIC` → `Decimal`, never float (§9).

    The returned `balance` is in each account's natural reading: for `credit` it is
    what is *owed* (positive while in debt), the asset figure negated — the §18 sign
    convention. Ordered default-first, then by id. Each dict carries `account_id`,
    `name`, `kind`, `is_default`, `balance`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.account_id, a.name, a.kind, a.is_default,"
            "  (CASE WHEN a.kind = 'credit' THEN -1 ELSE 1 END) * ("
            "     a.opening_balance"
            "     + coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'income'   AND account_id = a.account_id), 0)"
            "     - coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'expense'  AND account_id = a.account_id), 0)"
            "     + coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'transfer' AND to_account_id   = a.account_id), 0)"
            "     - coalesce((SELECT sum(amount) FROM active_transactions"
            "                 WHERE type = 'transfer' AND from_account_id = a.account_id), 0)"
            "  ) AS balance"
            " FROM accounts a"
            " WHERE a.household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            "   AND a.deleted_at IS NULL"
            " ORDER BY a.is_default DESC, a.account_id",
            (user_id,),
        )
        return [
            {
                "account_id": account_id,
                "name": name,
                "kind": kind,
                "is_default": is_default,
                "balance": balance,
            }
            for account_id, name, kind, is_default, balance in cur.fetchall()
        ]
