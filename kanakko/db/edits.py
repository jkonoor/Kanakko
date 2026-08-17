"""The dashboard's per-row mutations (§6, §13, §16, §17).

Split off `reports.py` — that module is reads, this is writes. Every function
here writes an audit row inside its own transaction, like the bot-side pair in
`pending`, and is scoped to `user_id` so one user cannot change another's row
by guessing an id (§1) — §16: "Edit / delete a row: only the member who
entered it".
"""

import psycopg

from kanakko.db.audit import _record_event


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


def restore_transaction(
    conn: psycopg.Connection,
    user_id: int,
    txn_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Undo a soft-delete: clear `deleted_at` on one of `user_id`'s own rows (§6, §13, §17, task 1771).

    The dashboard's post-delete toast ("Deleted ₹500.00 · Undo") calls this
    instead of a confirm-before-delete dialog — §6 already made a soft delete
    recoverable in the schema, this is what actually reverses it. Unlike every
    other write in this module, the row this needs to find is exactly the one
    `active_transactions` hides, so the `WHERE` targets `transactions` directly
    — the one place in the codebase that is correct rather than a violation of
    "reads go through the view" (CLAUDE.md): a live row is never a candidate
    here, `deleted_at IS NOT NULL` is the whole point of the clause, and (unlike
    `soft_delete_transaction`'s "already gone" no-op) there is no view of
    deleted rows to read through instead. Still scoped to `user_id` and its
    `household_id`, so one user cannot revive another's deleted row (§1, §16). A
    row that is already live, or belongs to someone else, or does not exist,
    matches nothing and returns `None`.

    Mirrors `soft_delete_transaction`: the restore and its audit row (§17) share
    one `conn.transaction()`; `before` is `None` and `after` carries the
    restored fields, the opposite of a delete's `before`/`after`. Returns the
    restored row's `txn_id` and amount, or `None`. Does not commit — the caller
    owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE transactions SET deleted_at = NULL"
            " WHERE user_id = %s AND txn_id = %s AND deleted_at IS NOT NULL"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id, txn_id, user_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        tid, amount, type_, category, note, occurred_on = row
        after = {
            "amount": amount,
            "type": type_,
            "category": category,
            "note": note,
            "occurred_on": occurred_on,
        }
        _record_event(cur, txn_id=tid, user_id=user_id, action="restore",
                      before=None, after=after, source=source, update_id=update_id)
    return {"txn_id": tid, "amount": amount}


#: Columns the dashboard's `POST /app/edit` may change (task 974). A closed set,
#: not a caller-supplied string, so `field` can go straight into an f-string
#: without ever carrying attacker input into the SQL text.
EDITABLE_TRANSACTION_FIELDS = frozenset({"amount", "occurred_on", "note", "account_id"})


def edit_transaction_field(
    conn: psycopg.Connection,
    user_id: int,
    txn_id: int,
    field: str,
    value,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Set one field of one live transaction, scoped to `user_id` (§13, §16, §17, task 974).

    §5 rejected a field editor in the bot chat but named the Mini App transaction
    list as the cheaper replacement for anything older than the last entry — this
    is that editor's write side. `field` is one of `EDITABLE_TRANSACTION_FIELDS`;
    the caller (`app.mini_app_edit`) has already parsed `value` to the right type
    (`Decimal` for amount, `date` for occurred_on) so this stays a plain UPDATE,
    same shape as `set_transaction_category`. Scoped to `user_id` so one user
    cannot edit another's row by guessing an id (§1) — §16: "Edit / delete a row:
    only the member who entered it". Both reads of the view carry the user's
    `household_id` too, so, like every read, they can never span households.

    `account_id` gets one more rule an f-string column name can't express: a
    `transfer` names two ends (`from_account_id`/`to_account_id`, §18) and no
    single `account_id` — the dashboard never offers the control on a transfer
    row, but a forged request could still try, so it is refused here as if the
    row did not match rather than trusted from the client. And the new id must
    resolve to one of *this* household's live, non-`external` accounts, or a
    forged id could relocate a transaction into another household's account or
    the structural `external` one.

    Old/new values flank an `edit` audit row (§17) in the same transaction as the
    UPDATE, mirroring `set_transaction_category`. Returns `{txn_id, amount}` or
    `None` when no live row of this user's matches, or the edit does not apply
    (a non-existent field value, a transfer's account, a foreign/deleted/external
    account id). Does not commit — the caller owns the transaction.
    """
    if field not in EDITABLE_TRANSACTION_FIELDS:
        raise ValueError(f"not an editable field: {field!r}")

    account_check = ""
    account_params: tuple = ()
    if field == "account_id":
        account_check = (
            " AND EXISTS (SELECT 1 FROM accounts"
            "   WHERE account_id = %s AND kind <> 'external' AND deleted_at IS NULL"
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s))"
        )
        account_params = (value, user_id)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"SELECT {field}, type FROM active_transactions"
            " WHERE user_id = %s AND txn_id = %s"
            " AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)",
            (user_id, txn_id, user_id),
        )
        old = cur.fetchone()
        if old is None:
            return None
        old_value, type_ = old
        if field == "account_id" and type_ == "transfer":
            return None
        cur.execute(
            f"UPDATE transactions SET {field} = %s"
            " WHERE txn_id = ("
            "   SELECT txn_id FROM active_transactions"
            "   WHERE user_id = %s AND txn_id = %s"
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            " )" + account_check +
            " RETURNING txn_id, amount",
            (value, user_id, txn_id, user_id) + account_params,
        )
        updated = cur.fetchone()
        if updated is None:
            return None
        tid, amount = updated
        _record_event(cur, txn_id=tid, user_id=user_id, action="edit",
                      before={field: old_value}, after={field: value},
                      source=source, update_id=update_id)
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
