"""The confirm-flow persistence, against a real Postgres (see conftest.py).

Task 56's core: a pending row becomes a stored transaction and is cleared, in
one step. The `conn` fixture is module-scoped and shared, so each test ends with
`conn.rollback()`; the schema itself is committed by `migrate()` and survives.
"""

from datetime import date
from decimal import Decimal

from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.db import confirm_pending, save_pending
from kanakko.migrate import migrate
from kanakko.parse import Transaction


def _seed_user(conn, telegram_user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING user_id",
            (telegram_user_id,),
        )
        (user_id,) = cur.fetchone()
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

    txn_id = confirm_pending(conn, user_id, 555)
    assert isinstance(txn_id, int)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, type, category, note, occurred_on"
            " FROM active_transactions WHERE txn_id = %s",
            (txn_id,),
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


def test_confirm_is_idempotent_on_redelivery(conn):
    """A second Confirm tap for the same card writes nothing more.

    Telegram redelivers taps it already got a 200 for. The pending row is gone
    after the first confirm, so the second returns `None` and the ledger still
    holds exactly one row for that user.
    """
    migrate(conn)
    user_id = _seed_user(conn, 11)
    save_pending(conn, user_id, 777, _txn())

    first = confirm_pending(conn, user_id, 777)
    second = confirm_pending(conn, user_id, 777)
    assert isinstance(first, int)
    assert second is None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone() == (1,)
    conn.rollback()


def test_confirm_unknown_message_returns_none(conn):
    """Confirming a card with no pending row is a no-op, not an error."""
    migrate(conn)
    assert confirm_pending(conn, 42, 999_999) is None
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

    txn_id = confirm_pending(conn, a, 555)
    assert isinstance(txn_id, int)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, user_id FROM active_transactions WHERE txn_id = %s", (txn_id,)
        )
        assert cur.fetchone() == (Decimal("100.00"), a)  # A's own amount, under A
        cur.execute("SELECT user_id FROM pending_transactions WHERE telegram_message_id = 555")
        assert cur.fetchone() == (b,)  # B's pending row still live, A's cleared
    conn.rollback()
