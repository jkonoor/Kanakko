"""Recurring rules for auto-debits (DECISIONS §18).

§18: "A recurring rule (amount, category, account, day of month, active) is
the answer to auto-debits... on the day, the cron sends the ordinary confirm
card... not a silent insert." `create_recurring_rule`/`list_recurring_rules`/
`set_recurring_rule_active`/`delete_recurring_rule` are the CRUD side —
household-scoped like `accounts.py` and `refunds.py` (§16's shared ledger means
any member may manage a rule, not just the one who made it). `due_rules_today`
is the cron's read: `kanakko.jobs.recurring` calls it, builds a confirm card
per row, and threads the rule id through `db.pending.save_pending` (§18) —
the actual creation surface for a rule (a bot command or a dashboard form) is
still undecided and is a separate task.
"""

from decimal import Decimal

import psycopg


def create_recurring_rule(
    conn: psycopg.Connection,
    user_id: int,
    account_id: int,
    category: str,
    amount: Decimal,
    day_of_month: int,
) -> dict | None:
    """Create a recurring rule in `user_id`'s household (§18).

    `account_id` must resolve to a live, non-`external` account in the
    caller's own household — the same shape `edits.edit_transaction_field`
    uses to refuse a forged account id — so a rule can't be pinned to another
    household's account or the structural counterparty by guessing an id.
    `category` is trusted here, the way `set_account_opening_balance` trusts
    `kind`: the caller validates it against the closed set (`categories.py`)
    before this runs. Returns the stored row, or `None` when the account does
    not resolve. Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recurring_rules"
            " (household_id, account_id, category, amount, day_of_month, created_by)"
            " SELECT hm.household_id, a.account_id, %s, %s, %s, %s"
            " FROM household_members hm"
            " JOIN accounts a ON a.household_id = hm.household_id AND a.account_id = %s"
            "   AND a.kind <> 'external' AND a.deleted_at IS NULL"
            " WHERE hm.user_id = %s"
            " RETURNING rule_id, household_id",
            (category, amount, day_of_month, user_id, account_id, user_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    rule_id, household_id = row
    return {
        "rule_id": rule_id,
        "household_id": household_id,
        "account_id": account_id,
        "category": category,
        "amount": amount,
        "day_of_month": day_of_month,
        "active": True,
    }


def list_recurring_rules(conn: psycopg.Connection, user_id: int) -> list[dict]:
    """Every recurring rule in `user_id`'s household, paused or not (§16, §18).

    Household-scoped like `account_balances` — every member sees every rule.
    Joined to the account's name, since a rule reads as "SIP, ₹5,000 on the
    5th", not "account 7". Ordered by day of month then id. Includes paused
    rules — the dashboard's pause toggle needs to see one to offer resuming
    it, not just the active ones the cron will act on.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.rule_id, r.account_id, a.name, r.category, r.amount,"
            " r.day_of_month, r.active"
            " FROM recurring_rules r"
            " JOIN accounts a ON a.account_id = r.account_id"
            " WHERE r.household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " ORDER BY r.day_of_month, r.rule_id",
            (user_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "rule_id": rule_id,
            "account_id": account_id,
            "account_name": account_name,
            "category": category,
            "amount": amount,
            "day_of_month": day_of_month,
            "active": active,
        }
        for rule_id, account_id, account_name, category, amount, day_of_month, active in rows
    ]


def set_recurring_rule_active(
    conn: psycopg.Connection, user_id: int, rule_id: int, active: bool
) -> dict | None:
    """Pause or resume a rule, scoped to `user_id`'s household (§16, §18).

    Household-scoped, not creator-scoped — the same reach `create_refund` has:
    any member may pause a rule another member set up. Returns
    `{rule_id, active}`, or `None` when `rule_id` does not resolve within the
    caller's household (foreign or non-existent). Does not commit — the caller
    owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE recurring_rules SET active = %s"
            " WHERE rule_id = %s"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " RETURNING rule_id, active",
            (active, rule_id, user_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    rid, is_active = row
    return {"rule_id": rid, "active": is_active}


def due_rules_today(conn: psycopg.Connection, day_of_month: int) -> list[dict]:
    """Every active rule whose `day_of_month` matches, across every household (§18).

    Cross-household like `db.users.all_users` — the cron reaches every
    household in one run, not one caller's. Joined to the account name (a card
    reads "SIP, Bank", not "account 7") and the rule creator's Telegram id, the
    send target (§18: the cron sends *a* confirm card, and the one who set the
    rule up is who is expecting it). The account join filters `deleted_at IS
    NULL` — a rule pinned to an account soft-deleted after creation simply
    doesn't come up as due, the same silent-skip the recurring_rules_due_idx
    partial index already assumes for a paused rule.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.rule_id, r.account_id, a.name, r.category, r.amount,"
            " r.created_by, u.telegram_user_id"
            " FROM recurring_rules r"
            " JOIN accounts a ON a.account_id = r.account_id AND a.deleted_at IS NULL"
            " JOIN users u ON u.user_id = r.created_by"
            " WHERE r.active AND r.day_of_month = %s"
            " ORDER BY r.rule_id",
            (day_of_month,),
        )
        rows = cur.fetchall()
    return [
        {
            "rule_id": rule_id,
            "account_id": account_id,
            "account_name": account_name,
            "category": category,
            "amount": amount,
            "created_by": created_by,
            "telegram_user_id": telegram_user_id,
        }
        for rule_id, account_id, account_name, category, amount, created_by, telegram_user_id in rows
    ]


def delete_recurring_rule(conn: psycopg.Connection, user_id: int, rule_id: int) -> dict | None:
    """Remove a rule outright, scoped to `user_id`'s household (§16, §18).

    A hard delete, not §6's soft delete: a recurring rule is a standing
    instruction, not a ledger row, and nothing yet references a `rule_id`
    (migration 015's comment), so there is nothing to orphan by removing it.
    Household-scoped like pause/resume above. Returns `{rule_id}`, or `None`
    when `rule_id` does not resolve within the caller's household. Does not
    commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM recurring_rules"
            " WHERE rule_id = %s"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " RETURNING rule_id",
            (rule_id, user_id),
        )
        row = cur.fetchone()
    return {"rule_id": row[0]} if row is not None else None
