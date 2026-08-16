"""Household-scoped ledger reads (§6, §16).

Every read here goes through `active_transactions` (§6) and carries a
`household_id` predicate so it can never span households (§16). Sums come back as
`NUMERIC` → `Decimal`, never float (§9). The dashboard's per-row mutations
(`soft_delete_transaction`, `restore_transaction`, `set_transaction_category`,
`edit_transaction_field`) are `kanakko.db.edits` — reads and writes are the
seam this module split on.
"""

from datetime import date
from decimal import Decimal

import psycopg


def day_summary(
    conn: psycopg.Connection, user_id: int, day: date
) -> tuple[int, Decimal, Decimal]:
    """`(entry_count, spent, received)` for `user_id`'s household on `day` (§6, §9, §12, §16).

    A household figure, not a personal one: the money belongs to the household, so
    the total is every member's live rows, not just the caller's (§16). `user_id`
    names who is asking; the query resolves their household from
    `household_members` and scopes on `household_id` — every read carries a
    household scope so it can never span households (§16). Buckets on `occurred_on`
    — when the money moved, not when it was logged — and reads `active_transactions`
    so a soft-deleted row never re-enters a total. `spent` nets out that day's
    `refund` rows (§18: "reduces the original's category total, never income") —
    a refund's own `occurred_on` is when it was refunded, which may fall in a
    different day than the expense it refunds. The two sums come back as
    `NUMERIC` → `Decimal` (never float, §9); an empty day yields `(0, 0.00, 0.00)`.
    `day` is a plain date the caller computes in `Asia/Kolkata` (§10), keeping the
    timezone boundary in one testable place.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*),"
            " coalesce(sum(amount) FILTER (WHERE type = 'expense'), 0)"
            "  - coalesce(sum(amount) FILTER (WHERE type = 'refund'), 0),"
            " coalesce(sum(amount) FILTER (WHERE type = 'income'), 0)"
            " FROM active_transactions"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND occurred_on = %s",
            (user_id, day),
        )
        count, spent, received = cur.fetchone()
    return count, spent, received


def month_summary(
    conn: psycopg.Connection, user_id: int, first_day: date, next_first_day: date
) -> tuple[Decimal, Decimal, list[tuple[str, Decimal]]]:
    """`(income, expenses, top_categories)` for `user_id`'s household in the month (§6, §9, §12, §16).

    A household figure (§16): scoped on the `household_id` resolved from `user_id`'s
    `household_members` row, so it totals every member's entries, and can never span
    households. The range is half-open `[first_day, next_first_day)` on `occurred_on`
    — when the money moved — so a 23:50 IST entry on the month's last day lands in
    that month (the caller computes both boundaries in `Asia/Kolkata`, §10). Reads
    `active_transactions`, so a soft-deleted row never re-enters the totals (§6).
    `expenses` and each category total net out the month's `refund` rows (§18:
    "reduces the original's category total, never income") — a refund carries its
    original transaction's category (`refunds.create_refund` copies it at write
    time), so a plain `GROUP BY category` nets it without joining back to the
    refunded row. The two sums come back as `NUMERIC` → `Decimal` (never float,
    §9). Top categories are the month's expense categories with their totals,
    biggest first, fully-refunded ones dropped — the caller decides how many to
    show.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT"
            " coalesce(sum(amount) FILTER (WHERE type = 'income'), 0),"
            " coalesce(sum(amount) FILTER (WHERE type = 'expense'), 0)"
            "  - coalesce(sum(amount) FILTER (WHERE type = 'refund'), 0)"
            " FROM active_transactions"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND occurred_on >= %s AND occurred_on < %s",
            (user_id, first_day, next_first_day),
        )
        income, expenses = cur.fetchone()
        cur.execute(
            "SELECT category,"
            " sum(CASE WHEN type = 'expense' THEN amount ELSE -amount END)"
            " FROM active_transactions"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND occurred_on >= %s AND occurred_on < %s"
            " AND type IN ('expense', 'refund')"
            " GROUP BY category"
            " HAVING sum(CASE WHEN type = 'expense' THEN amount ELSE -amount END) <> 0"
            " ORDER BY sum(CASE WHEN type = 'expense' THEN amount ELSE -amount END) DESC, category",
            (user_id, first_day, next_first_day),
        )
        top = cur.fetchall()
    return income, expenses, top


def recent_transactions(
    conn: psycopg.Connection, user_id: int, limit: int = 10
) -> list[tuple]:
    """The household's most recent live transactions, newest first (§6, §13, §16).

    The dashboard's recent list: a household figure (§16), scoped on the
    `household_id` resolved from `user_id`'s `household_members` row, so it lists
    every member's entries and can never span households. Each row carries its
    `txn_id` so the per-row delete button can name it. Reads `active_transactions`,
    so a soft-deleted row never reappears (§6). `limit` caps the list — the
    dashboard shows a handful, not the whole ledger. Amounts come back as
    `NUMERIC` → `Decimal` (§9). Ordered by `created_at` (when logged) so the list
    matches the order entries were added. Columns 7/8 are the `from`/`to` account
    names — `NULL` for every type but `transfer` — joined in here so `recent_list`
    (§18) can render "Bank → SIP" without a second round trip. Column 9 is the
    row's own `account_id` — `NULL` for a `transfer` (it names two ends, not one,
    §18), otherwise the account the dashboard's edit control (task 974) pre-selects.
    Column 10 is `refunded_so_far` — the sum of this row's live refunds (0 unless
    it is a partially- or fully-refunded `expense`), the same "amount minus its
    live refunds" aggregate migration 014's trigger enforces and
    `refund_candidates` already computes. The dashboard's refund panel needs it to
    offer what actually remains rather than an amount the trigger will reject.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.txn_id, t.amount, t.type, t.category, t.note, t.occurred_on,"
            " fa.name, ta.name, t.account_id,"
            " coalesce((SELECT sum(r.amount) FROM active_transactions r"
            "  WHERE r.refund_of_txn_id = t.txn_id), 0)"
            " FROM active_transactions t"
            " LEFT JOIN accounts fa ON fa.account_id = t.from_account_id"
            " LEFT JOIN accounts ta ON ta.account_id = t.to_account_id"
            " WHERE t.household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " ORDER BY t.created_at DESC, t.txn_id DESC LIMIT %s",
            (user_id, limit),
        )
        return cur.fetchall()
