"""Household-scoped ledger reads and the dashboard's per-row mutations (§6, §16).

Every read here goes through `active_transactions` (§6) and carries a
`household_id` predicate so it can never span households (§16). Sums come back as
`NUMERIC` → `Decimal`, never float (§9). The two dashboard mutations
(`soft_delete_transaction`, `set_transaction_category`) write an audit row inside
their own transaction, like the bot-side pair in `pending`.
"""

from datetime import date
from decimal import Decimal

import psycopg

from kanakko.db.audit import _record_event


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
    so a soft-deleted row never re-enters a total. The two sums come back as
    `NUMERIC` → `Decimal` (never float, §9); an empty day yields `(0, 0.00, 0.00)`.
    `day` is a plain date the caller computes in `Asia/Kolkata` (§10), keeping the
    timezone boundary in one testable place.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*),"
            " coalesce(sum(amount) FILTER (WHERE type = 'expense'), 0),"
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
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND occurred_on >= %s AND occurred_on < %s",
            (user_id, first_day, next_first_day),
        )
        income, expenses = cur.fetchone()
        cur.execute(
            "SELECT category, sum(amount) FROM active_transactions"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " AND occurred_on >= %s AND occurred_on < %s"
            " AND type = 'expense'"
            " GROUP BY category ORDER BY sum(amount) DESC, category",
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
    matches the order entries were added.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT txn_id, amount, type, category, note, occurred_on"
            " FROM active_transactions"
            " WHERE household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
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
    `txn_id`, and `WHERE txn_id = NULL` matches nothing. The subquery also carries
    the user's `household_id`, so like every read of the view it can never span
    households (§16); here it is defensive (the `user_id` scope already confines to
    one household). Mirrors `undo_last`'s read-through-the-view pattern. The delete and its audit row (§17) share one
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
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " )"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id, txn_id, user_id),
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
    UPDATE matches nothing, returning `None` rather than a stray write. Both reads
    of the view also carry the user's `household_id` so, like every read, they can
    never span households (§16 — defensive here, the `user_id` scope already does).
    The caller validates `category` against the closed set (`categories.py`) before
    this runs. Mirrors `soft_delete_transaction`.

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
            " WHERE user_id = %s AND txn_id = %s"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)",
            (user_id, txn_id, user_id),
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
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " )"
            " RETURNING txn_id, amount",
            (category, user_id, txn_id, user_id),
        )
        tid, amount = cur.fetchone()
        _record_event(cur, txn_id=tid, user_id=user_id, action="recategorise",
                      before={"category": old_category}, after={"category": category},
                      source=source, update_id=update_id)
    return {"txn_id": tid, "amount": amount}
