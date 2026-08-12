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

from decimal import Decimal

import psycopg

# The two kinds onboarding lets a user set up beyond the default (§18) — `spending`
# is minted automatically and `external` is structural, so neither is user-created.
ACCOUNT_ONBOARDING_KINDS = {"credit": "Card", "locked": "Savings"}


def create_default_accounts(
    conn: psycopg.Connection, household_id: int, owner: int
) -> None:
    """Mint the two structural accounts every household needs (§18).

    A default `spending` account ('Bank') — "a transaction that names no account
    lands there" — and the `external` account, the counterparty for opening
    balances and adjustments. Called once, right after `households.household_id`
    is inserted (`create_household_of_one`), which only reaches here for a
    genuinely new household, so no existence guard is needed. Does not commit —
    the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name, is_default)"
            " VALUES (%s, %s, 'spending', 'Bank', true)",
            (household_id, owner),
        )
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'external', 'External')",
            (household_id, owner),
        )


def set_account_opening_balance(
    conn: psycopg.Connection, user_id: int, kind: str, amount: Decimal
) -> dict:
    """Create or update the caller's household's `kind` account with `amount` (§18).

    The onboarding ask: `amount` is always what the user reports positively — "how
    much you owe" for `credit`, "how much is already in it" for `locked` — and this
    is the one place that turns it into the signed `opening_balance` the ledger
    stores (§18: "same column, different question"). One account per kind per
    household: a second call updates the opening balance rather than minting a
    duplicate, so a typo is correctable. `kind` is trusted — the caller
    (`handlers.handle_account`) has already restricted it to `credit`/`locked`,
    the two kinds this command may create; `spending` and `external` are structural
    and never made this way. Returns `{account_id, kind, name}`. Does not commit —
    the caller owns the transaction.
    """
    name = ACCOUNT_ONBOARDING_KINDS[kind]
    signed = -amount if kind == "credit" else amount
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id FROM accounts"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            "   AND kind = %s AND deleted_at IS NULL",
            (user_id, kind),
        )
        row = cur.fetchone()
        if row is not None:
            (account_id,) = row
            cur.execute(
                "UPDATE accounts SET opening_balance = %s WHERE account_id = %s",
                (signed, account_id),
            )
        else:
            cur.execute(
                "INSERT INTO accounts (household_id, owner, kind, name, opening_balance)"
                " SELECT household_id, %s, %s, %s, %s"
                " FROM household_members WHERE user_id = %s"
                " RETURNING account_id",
                (user_id, kind, name, signed, user_id),
            )
            (account_id,) = cur.fetchone()
    return {"account_id": account_id, "kind": kind, "name": name}


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
