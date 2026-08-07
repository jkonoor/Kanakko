"""The confirm-flow persistence, against a real Postgres (see conftest.py).

Task 56's core: a pending row becomes a stored transaction and is cleared, in
one step. The `conn` fixture is module-scoped and shared, so each test ends with
`conn.rollback()`; the schema itself is committed by `migrate()` and survives.
"""

from datetime import date
from decimal import Decimal

import psycopg
import pytest
from conftest import household_of

from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.db import (
    confirm_pending,
    get_or_create_user,
    save_pending,
    set_pending_category,
    set_transaction_category,
    soft_delete_transaction,
    undo_last,
)
from kanakko.migrate import migrate
from kanakko.parse import Transaction


def _seed_user(conn, telegram_user_id: int) -> int:
    """A user in a household of one — confirm_pending homes the row there (§16)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING user_id",
            (telegram_user_id,),
        )
        (user_id,) = cur.fetchone()
    household_of(conn, user_id)
    return user_id


def _txn(amount: str = "1234.56") -> Transaction:
    return Transaction.model_validate(
        {
            "type": "expense",
            "amount": amount,
            "category": EXPENSE_CATEGORIES[0],
            "date": "2026-08-05",
            "note": "lunch at cafe",
        }
    )


def test_get_or_create_user_is_idempotent(conn):
    """Two messages from one Telegram user resolve to one `users` row.

    The second call must return the *same* internal id and create no duplicate —
    a plain INSERT (no `ON CONFLICT`) would raise a unique violation on the second
    message, and an unconditional insert would fork the user into two ids.
    """
    migrate(conn)
    first = get_or_create_user(conn, 90210)
    second = get_or_create_user(conn, 90210)
    other = get_or_create_user(conn, 90211)

    assert isinstance(first, int)
    assert second == first
    assert other != first

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = 90210")
        assert cur.fetchone() == (1,)
    conn.rollback()


def test_confirm_writes_the_transaction_and_clears_pending(conn):
    """Confirm stores the exact fields and removes the pending row.

    The amount comes back as an exact `Decimal`, not a float: `save_pending`
    stores it as a JSON string and `confirm_pending` reads it back through the
    `Transaction` validator (§9). Storing it as a JSON number instead would make
    that validator reject a float and this test error out — so the guard bites.
    """
    migrate(conn)
    user_id = _seed_user(conn, 10)

    pending_id = save_pending(conn, user_id, 555, _txn())
    assert isinstance(pending_id, int)

    row = confirm_pending(conn, user_id, 555, source="webhook", update_id=None)
    assert isinstance(row["txn_id"], int)
    assert row["amount"] == Decimal("1234.56")  # the row carries the amount (§17)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, type, category, note, occurred_on"
            " FROM active_transactions WHERE txn_id = %s",
            (row["txn_id"],),
        )
        assert cur.fetchone() == (
            Decimal("1234.56"),
            "expense",
            EXPENSE_CATEGORIES[0],
            "lunch at cafe",
            date(2026, 8, 5),
        )
        cur.execute("SELECT count(*) FROM pending_transactions WHERE telegram_message_id = 555")
        assert cur.fetchone() == (0,)
    conn.rollback()


def test_confirm_homes_the_transaction_in_the_confirmers_household(conn):
    """A confirmed row lands in the entering user's household, never another's (§16).

    The write path moves the tenancy axis: `confirm_pending` stamps `household_id`
    from the confirmer's `household_members` row. Two users in separate households
    must not cross-home each other's money. Homing it to the wrong household would
    redden the id assertion; dropping the subquery would redden the NOT NULL insert.
    """
    migrate(conn)
    a = _seed_user(conn, 73)
    b = _seed_user(conn, 74)
    ha = household_of(conn, a)
    hb = household_of(conn, b)
    assert ha != hb  # distinct households of one

    save_pending(conn, a, 730, _txn("100.00"))
    row = confirm_pending(conn, a, 730, source="webhook", update_id=None)

    with conn.cursor() as cur:
        cur.execute("SELECT household_id FROM transactions WHERE txn_id = %s", (row["txn_id"],))
        assert cur.fetchone() == (ha,)  # A's household, not B's
    conn.rollback()


def test_confirm_without_a_household_is_refused(conn):
    """A user with no household cannot confirm — money must have a home (§16).

    The household subquery yields NULL and migration 008's NOT NULL rejects the
    insert, rather than silently writing money that belongs to no household total.
    Reverting 008's `SET NOT NULL` reddens this — the confirm would succeed with a
    NULL household. A raw user seed is used (not `_seed_user`, which mints one).
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (75) RETURNING user_id")
        (uid,) = cur.fetchone()
    save_pending(conn, uid, 750, _txn("50.00"))

    with pytest.raises(psycopg.errors.NotNullViolation):
        confirm_pending(conn, uid, 750, source="webhook", update_id=None)
    conn.rollback()


def test_confirm_is_idempotent_on_redelivery(conn):
    """A second Confirm tap for the same card writes nothing more.

    Telegram redelivers taps it already got a 200 for. The pending row is gone
    after the first confirm, so the second returns `None` and the ledger still
    holds exactly one row for that user.
    """
    migrate(conn)
    user_id = _seed_user(conn, 11)
    save_pending(conn, user_id, 777, _txn())

    first = confirm_pending(conn, user_id, 777, source="webhook", update_id=None)
    second = confirm_pending(conn, user_id, 777, source="webhook", update_id=None)
    assert isinstance(first["txn_id"], int)
    assert second is None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone() == (1,)
    conn.rollback()


def test_confirm_unknown_message_returns_none(conn):
    """Confirming a card with no pending row is a no-op, not an error."""
    migrate(conn)
    assert confirm_pending(conn, 42, 999_999, source="webhook", update_id=None) is None
    conn.rollback()


def test_set_pending_category_updates_the_row(conn):
    """A category tap re-writes the pending row's category and returns the txn (§5).

    The stored `parsed` must carry the new category so the eventual Confirm writes
    it; the returned `Transaction` is what the handler re-renders. The amount is
    untouched and still a JSON string (§9) — a botched update that dropped it or
    turned it into a number would fail the `Transaction` round-trip.
    """
    migrate(conn)
    user_id = _seed_user(conn, 30)
    save_pending(conn, user_id, 555, _txn("250.00"))

    new_cat = EXPENSE_CATEGORIES[3]
    txn = set_pending_category(conn, user_id, 555, new_cat)
    assert txn is not None
    assert txn.category == new_cat
    assert txn.amount == Decimal("250.00")  # amount survives the update

    with conn.cursor() as cur:
        cur.execute(
            "SELECT parsed FROM pending_transactions WHERE telegram_message_id = 555"
        )
        (parsed,) = cur.fetchone()
    assert parsed["category"] == new_cat
    assert parsed["amount"] == "250.00"  # §9: still a string, not a float
    conn.rollback()


def test_set_pending_category_unknown_message_returns_none(conn):
    """Setting a category on a card with no pending row is a no-op, not an error."""
    migrate(conn)
    assert set_pending_category(conn, 42, 999_999, EXPENSE_CATEGORIES[0]) is None
    conn.rollback()


def test_set_pending_category_is_scoped_to_the_user(conn):
    """One user's category tap must not rewrite another user's identical card (§1).

    A and B both hold a pending card on message id 555. A is seeded first (the
    older row), so the fallback `ORDER BY … DESC` picks B's newer row — only the
    `user_id` clause makes A's tap resolve to A's row. Dropping it reddens this.
    """
    migrate(conn)
    a = _seed_user(conn, 40)
    b = _seed_user(conn, 41)
    save_pending(conn, a, 555, _txn("100.00"))
    save_pending(conn, b, 555, _txn("999.99"))

    set_pending_category(conn, a, 555, EXPENSE_CATEGORIES[3])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = 555",
            (b,),
        )
        (parsed,) = cur.fetchone()
    assert parsed["category"] == EXPENSE_CATEGORIES[0]  # B's row untouched
    conn.rollback()


def _confirm(conn, user_id: int, message_id: int, amount: str) -> int:
    save_pending(conn, user_id, message_id, _txn(amount))
    return confirm_pending(
        conn, user_id, message_id, source="webhook", update_id=None
    )["txn_id"]


def test_undo_soft_deletes_the_most_recent_and_returns_it(conn):
    """`/undo` sets `deleted_at` on the newest live row and reports its fields (§5, §6).

    The row is soft-deleted, not hard-deleted — it vanishes from
    `active_transactions` (§6) but still exists in `transactions` with a
    `deleted_at`, keeping the ledger recoverable. Older entries are untouched, and
    the returned amount is an exact `Decimal` (§9) for the confirmation message.
    """
    migrate(conn)
    user_id = _seed_user(conn, 50)
    _confirm(conn, user_id, 101, "100.00")
    newest = _confirm(conn, user_id, 102, "250.00")

    removed = undo_last(conn, user_id, source="webhook", update_id=None)
    assert removed is not None
    assert removed["amount"] == Decimal("250.00")  # §9: exact Decimal, the newest
    assert removed["type"] == "expense"
    assert removed["category"] == EXPENSE_CATEGORIES[0]

    with conn.cursor() as cur:
        cur.execute("SELECT deleted_at FROM transactions WHERE txn_id = %s", (newest,))
        (deleted_at,) = cur.fetchone()
        assert deleted_at is not None  # soft-deleted, still present in transactions
        cur.execute(
            "SELECT amount FROM active_transactions WHERE user_id = %s", (user_id,)
        )
        assert cur.fetchall() == [(Decimal("100.00"),)]  # only the older row is live
    conn.rollback()


def test_undo_walks_back_through_history(conn):
    """A second `/undo` removes the *previous* entry, never re-deletes the newest (§6).

    Because `undo_last` chooses from `active_transactions`, which hides the row the
    first `/undo` soft-deleted, the second `/undo` picks the next-newest. Reading
    `transactions` directly would keep latching onto the already-deleted newest row
    and the older ones would be unreachable — so this reddens the moment the SELECT
    stops going through the view.
    """
    migrate(conn)
    user_id = _seed_user(conn, 51)
    _confirm(conn, user_id, 101, "100.00")
    _confirm(conn, user_id, 102, "250.00")

    first = undo_last(conn, user_id, source="webhook", update_id=None)
    second = undo_last(conn, user_id, source="webhook", update_id=None)
    assert first["amount"] == Decimal("250.00")  # newest first
    assert second["amount"] == Decimal("100.00")  # then the previous one, not a repeat

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone() == (0,)  # both walked back
        assert undo_last(conn, user_id, source="webhook", update_id=None) is None
    conn.rollback()


def test_undo_with_nothing_to_undo_returns_none(conn):
    """`/undo` for a user with no live transaction is a no-op, not an error."""
    migrate(conn)
    assert undo_last(conn, _seed_user(conn, 52), source="webhook", update_id=None) is None
    conn.rollback()


def test_undo_is_scoped_to_the_user(conn):
    """One user's `/undo` must not touch another user's most recent row (§1)."""
    migrate(conn)
    a = _seed_user(conn, 60)
    b = _seed_user(conn, 61)
    _confirm(conn, a, 555, "100.00")
    _confirm(conn, b, 555, "999.99")

    removed = undo_last(conn, a, source="webhook", update_id=None)
    assert removed["amount"] == Decimal("100.00")  # A's own row, not B's newer one

    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM active_transactions WHERE user_id = %s", (b,))
        assert cur.fetchall() == [(Decimal("999.99"),)]  # B's row still live
    conn.rollback()


def test_confirm_is_scoped_to_the_user(conn):
    """One user's Confirm must not write another user's identically-numbered card.

    Telegram message ids repeat per chat (§1), so users A and B can both have a
    live pending card on message id 555. A confirms *their own* card and must get
    exactly their own amount (₹100), with only their pending row cleared.

    A is seeded *first*, so their pending row is the older one — the fallback
    `ORDER BY created_at DESC, pending_id DESC` picks B's newer row, not A's.
    Keyed on the message id alone, A's Confirm would therefore latch onto B's
    row: write B's ₹999.99 and delete B's pending, leaving A's live. Scoping the
    SELECT by `user_id` is the only thing that makes A's tap resolve to A's row —
    so this test reddens the moment that clause is dropped.
    """
    migrate(conn)
    a = _seed_user(conn, 20)
    b = _seed_user(conn, 21)
    save_pending(conn, a, 555, _txn("100.00"))
    save_pending(conn, b, 555, _txn("999.99"))

    row = confirm_pending(conn, a, 555, source="webhook", update_id=None)
    assert isinstance(row["txn_id"], int)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, user_id FROM active_transactions WHERE txn_id = %s",
            (row["txn_id"],),
        )
        assert cur.fetchone() == (Decimal("100.00"), a)  # A's own amount, under A
        cur.execute("SELECT user_id FROM pending_transactions WHERE telegram_message_id = 555")
        assert cur.fetchone() == (b,)  # B's pending row still live, A's cleared
    conn.rollback()


# --- §17 audit trail: transaction_events, written in db.py inside the money tx ---


def _events(conn, txn_id: int) -> list[tuple]:
    """`(action, before, after, source, update_id)` for `txn_id`, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, before, after, source, update_id"
            " FROM transaction_events WHERE txn_id = %s ORDER BY created_at, event_id",
            (txn_id,),
        )
        return cur.fetchall()


def test_confirm_writes_an_audit_row(conn):
    """Confirm writes one `transaction.confirmed` audit row atop the ledger row (§17).

    `before` is null (nothing existed), `after` carries the row's fields with the
    amount as its canonical string (§9, never a float), and the correlation id is
    captured. Removing the audit write in `confirm_pending` reddens this.
    """
    migrate(conn)
    user_id = _seed_user(conn, 80)
    save_pending(conn, user_id, 800, _txn("250.00"))

    row = confirm_pending(conn, user_id, 800, source="webhook", update_id=42)
    events = _events(conn, row["txn_id"])
    assert len(events) == 1
    action, before, after, source, update_id = events[0]
    assert action == "confirm"
    assert before is None
    assert after["amount"] == "250.00" and after["type"] == "expense"
    assert source == "webhook" and update_id == 42
    conn.rollback()


def test_recategorise_records_the_before_and_after_category(conn):
    """A confirmed row's category change records both sides (§17's missing data).

    Postgres 16 has no `RETURNING OLD.*`, so `set_transaction_category` reads the
    old category with a `SELECT` before its `UPDATE`, in the same transaction —
    this proves that read lands in `before` while the new value lands in `after`.
    """
    migrate(conn)
    user_id = _seed_user(conn, 81)
    txn_id = _confirm(conn, user_id, 810, "100.00")  # category = EXPENSE_CATEGORIES[0]

    new_cat = EXPENSE_CATEGORIES[3]
    updated = set_transaction_category(
        conn, user_id, txn_id, new_cat, source="miniapp", update_id=None
    )
    assert updated["txn_id"] == txn_id

    events = _events(conn, txn_id)
    assert [e[0] for e in events] == ["confirm", "recategorise"]
    _, before, after, source, update_id = events[-1]
    assert before == {"category": EXPENSE_CATEGORIES[0]}  # the SELECT-before-UPDATE
    assert after == {"category": new_cat}
    assert source == "miniapp" and update_id is None  # a Mini App event, §17 gap 2
    conn.rollback()


def test_undo_and_delete_write_audit_rows(conn):
    """Both soft-delete paths record the removed row as `before`, with no `after` (§17).

    `undo` (webhook, carries an `update_id`) and dashboard `delete` (miniapp, no
    `update_id`) each hang an audit row off the row they touched.
    """
    migrate(conn)
    user_id = _seed_user(conn, 82)
    t1 = _confirm(conn, user_id, 820, "30.00")
    t2 = _confirm(conn, user_id, 821, "40.00")

    undo_last(conn, user_id, source="webhook", update_id=99)  # undoes the newest, t2
    soft_delete_transaction(conn, user_id, t1, source="miniapp", update_id=None)

    e2 = _events(conn, t2)
    assert [e[0] for e in e2] == ["confirm", "undo"]
    assert e2[-1][1]["amount"] == "40.00"  # before carries the removed row
    assert e2[-1][2] is None  # an undo has no after
    assert e2[-1][3] == "webhook" and e2[-1][4] == 99

    e1 = _events(conn, t1)
    assert [e[0] for e in e1] == ["confirm", "delete"]
    assert e1[-1][1]["amount"] == "30.00"
    assert e1[-1][2] is None
    assert e1[-1][3] == "miniapp" and e1[-1][4] is None
    conn.rollback()


def test_the_ledger_row_and_its_audit_row_are_atomic(conn):
    """A handler raising after the ledger write leaves NEITHER row (§17).

    The audit row shares the money statement's `conn.transaction()` in `db.py`, so
    a confirmed transaction can never exist without a record of who created it —
    the silent, production-only failure §17 warns of. The webhook owns the outer
    transaction and rolls it all back on a handler failure; this simulates that.
    Reddens the moment either write is committed independently of the other.
    """
    migrate(conn)
    user_id = _seed_user(conn, 83)
    save_pending(conn, user_id, 830, _txn("77.00"))

    class Boom(Exception):
        pass

    try:
        with conn.transaction():  # the outer transaction the webhook owns
            confirm_pending(conn, user_id, 830, source="webhook", update_id=1)
            raise Boom  # a handler failing after the ledger write
    except Boom:
        pass

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone() == (0,)  # no orphaned ledger row
        cur.execute(
            "SELECT count(*) FROM transaction_events WHERE user_id = %s", (user_id,)
        )
        assert cur.fetchone() == (0,)  # and no orphaned audit row
    conn.rollback()
