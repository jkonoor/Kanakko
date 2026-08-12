"""Reconciliation adjustments: the ledger meets the real world (§18).

Nothing is connected to a bank, so the ledger drifts. §18's fix is a nudge
("what does your bank say?") plus, when the answer disagrees, a **visible**
adjustment row against the `external` account — never a silent rewrite of a
balance. `create_adjustment` is that write. `accounts_for_reconcile` is the
weekly cron's read (every household's own reconcilable accounts, cross-household
like `db.due_rules_today`); `create_reconcile_ask`/`pending_awaiting_reconcile`/
`clear_reconcile_ask` are the "awaiting a reply" state the nudge and its answer
share — the same `pending_transactions` row 017's `awaiting_amount` used for
"Change amount", not a new table (migration 019 made `parsed` nullable for
exactly this: a reconcile ask holds no `Transaction`, only which account it asks
about).
"""

from datetime import date
from decimal import Decimal

import psycopg

from kanakko.db.audit import _record_event


def accounts_for_reconcile(conn: psycopg.Connection) -> list[dict]:
    """Every live, non-`external` account across every household, with its owner (§18).

    Cross-household like `db.recurring.due_rules_today` — the weekly cron reaches
    every household in one run, not one caller's. `external` is structural
    bookkeeping and never something a nudge is sent for (the same exclusion
    `create_adjustment` enforces on the write side). Joined to the owner's
    Telegram id, the send target — §18 names the account's owning member, not
    every household member, as who is asked.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.account_id, a.household_id, a.name, a.kind, a.owner, u.telegram_user_id"
            " FROM accounts a"
            " JOIN users u ON u.user_id = a.owner"
            " WHERE a.kind <> 'external' AND a.deleted_at IS NULL"
            " ORDER BY a.household_id, a.account_id",
        )
        rows = cur.fetchall()
    return [
        {
            "account_id": account_id,
            "household_id": household_id,
            "name": name,
            "kind": kind,
            "owner": owner,
            "telegram_user_id": telegram_user_id,
        }
        for account_id, household_id, name, kind, owner, telegram_user_id in rows
    ]


def create_reconcile_ask(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, account_id: int
) -> int:
    """Mark the nudge just sent as awaiting a reply, keyed by its own message id (§18).

    A `pending_transactions` row with no `parsed` `Transaction` — migration 019's
    CHECK requires exactly one of `parsed` or `awaiting_reconcile_account_id`, and
    a reconcile ask is never a transaction. `pending_awaiting_reconcile` is the
    read this write feeds: `app.py`'s webhook checks it for every `TextMessage`,
    the same way it already does for `pending_awaiting_amount`. Returns the new
    `pending_id`. Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_transactions"
            " (user_id, telegram_message_id, awaiting_reconcile_account_id)"
            " VALUES (%s, %s, %s) RETURNING pending_id",
            (user_id, telegram_message_id, account_id),
        )
        (pending_id,) = cur.fetchone()
    return pending_id


def pending_awaiting_reconcile(
    conn: psycopg.Connection, user_id: int, reply_to_message_id: int | None = None
) -> dict | None:
    """The one outstanding reconcile ask this `TextMessage` is answering, or `None` (§18).

    `pending_awaiting_amount`'s counterpart for the reconcile flow — checked
    before routing a `TextMessage`, so the reply lands on `reconcile_flow`
    instead of a fresh parse. A household can have more than one account
    reconcilable at once, so more than one ask can be outstanding at once — a
    plain "most recent" pick routes a reply to whichever nudge is newest instead
    of the one it was typed to answer (QA finding, `104d562` review). So:
    when the reply is a Telegram "Reply" naming a nudge (`reply_to_message_id`
    is that nudge's own `telegram_message_id`), that ask and only that ask
    matches — a reply aimed at an unrelated message returns `None` rather than
    guessing. A bare message with no reply target only resolves when exactly one
    ask is outstanding, the case every existing single-account test covers;
    with more than one outstanding it returns `None` rather than silently
    picking one, so the reply falls through to ordinary parsing instead of
    landing on the wrong account. Returns `{telegram_message_id, account_id}`,
    or `None` when nothing matches.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id, awaiting_reconcile_account_id"
            " FROM pending_transactions"
            " WHERE user_id = %s AND awaiting_reconcile_account_id IS NOT NULL"
            " ORDER BY created_at DESC, pending_id DESC",
            (user_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    if reply_to_message_id is not None:
        for telegram_message_id, account_id in rows:
            if telegram_message_id == reply_to_message_id:
                return {"telegram_message_id": telegram_message_id, "account_id": account_id}
        return None
    if len(rows) > 1:
        return None
    telegram_message_id, account_id = rows[0]
    return {"telegram_message_id": telegram_message_id, "account_id": account_id}


def clear_reconcile_ask(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int
) -> int | None:
    """Discard the user's outstanding reconcile ask for `telegram_message_id` (§18).

    `reconcile_flow.handle_reconcile_reply`'s write, once a reply has been read as
    an amount — a parse failure leaves the row awaiting, the same "just retry"
    contract `set_pending_amount` gives "Change amount". Scoped by `user_id` like
    every other pending write (§1). Returns the cleared `pending_id`, or `None`
    when there was nothing to clear (a stale or already-answered nudge). Does not
    commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            "   AND awaiting_reconcile_account_id IS NOT NULL"
            " RETURNING pending_id",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


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
