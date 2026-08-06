"""The runner against a real Postgres, which is the only thing that parses DDL.

`tests/test_migrations.py` reads the `.sql` files as text; this one executes
them, in a throwaway cluster booted in a tmp dir. It deliberately offers no
"point me at your own database" switch: the first assertion below only holds
against a virgin database, and `migrate()` commits, so a persistent DSN would
pass once and then fail on every later run.
"""

from decimal import Decimal

import pytest

from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.migrate import MIGRATIONS, migrate
from kanakko.money import parse_amount

# The `conn` fixture (a throwaway Postgres cluster) lives in tests/conftest.py.


def test_migrate_refuses_to_succeed_with_no_migrations(tmp_path, monkeypatch):
    """Finding nothing to apply is a packaging failure, not a clean run.

    `MIGRATIONS` resolves relative to the installed module, so a wheel built
    without `migrations/` makes the glob empty. Without this guard the migrator
    exits 0 against an empty schema and the deploy looks green. No server
    needed — the guard runs before the connection is touched, which is why this
    check can't be skipped on a machine with no Postgres.
    """
    monkeypatch.setattr("kanakko.migrate.MIGRATIONS", tmp_path)
    with pytest.raises(RuntimeError, match=str(tmp_path)):
        migrate(None)


def test_migrations_apply_and_are_recorded(conn):
    """The first run executes the DDL; the second must be a no-op.

    Re-running is the normal case — every container restart calls this — and a
    runner that forgets what it applied errors on the restart, not on the
    change that broke it.
    """
    names = sorted(sql.name for sql in MIGRATIONS.glob("*.sql"))
    assert migrate(conn) == names
    assert migrate(conn) == []

    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at, filename")
        assert [row[0] for row in cur.fetchall()] == names


def test_schema_stores_money_exactly(conn):
    """Round-trips through the applied schema, not through the .sql text.

    `NUMERIC(12, 2)` is only a promise until a server enforces it; a column
    that came back as float would round here and nowhere else.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (1) RETURNING user_id")
        (user_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO transactions (user_id, amount, type, occurred_on)"
            " VALUES (%s, %s, 'expense', '2026-08-05') RETURNING txn_id",
            (user_id, Decimal("1234.56")),
        )
        (txn_id,) = cur.fetchone()

        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone() == (Decimal("1234.56"),)

        cur.execute("UPDATE transactions SET deleted_at = now() WHERE txn_id = %s", (txn_id,))
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone() == (0,)
    conn.rollback()


def test_parse_store_sum_by_category_stays_exact(conn):
    """The whole money path — parse → store → SUM(amount) GROUP BY category.

    The amounts are chosen so a `float` pipeline would drift: 0.10 + 0.20 + 0.30
    is 0.6000000000000001 in binary floating point, and 10.10 + 20.20 + 0.05 is
    30.349999999999998. Through `parse_amount` (Decimal) into `NUMERIC(12, 2)`
    and back through Postgres SUM, both come back exact. A column that had become
    float, or a Python-side float sum, reddens the equality — and summing over
    `active_transactions` (never `transactions`, DECISIONS §6) means a
    soft-deleted row must not land in a category total.
    """
    migrate(conn)
    food, groceries = EXPENSE_CATEGORIES[0], EXPENSE_CATEGORIES[1]
    entries = [
        (food, "₹0.10"),
        (food, "0.20"),
        (food, "0.30"),
        (groceries, "10.10"),
        (groceries, "20.20"),
        (groceries, "0.05"),
    ]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (2) RETURNING user_id")
        (user_id,) = cur.fetchone()
        for category, raw in entries:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, occurred_on)"
                " VALUES (%s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
                (user_id, parse_amount(raw), category),
            )
        # A soft-deleted Food row must not reach the Food total.
        cur.execute(
            "INSERT INTO transactions (user_id, amount, type, category, occurred_on)"
            " VALUES (%s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
            (user_id, parse_amount("999.99"), food),
        )
        (deleted_txn,) = cur.fetchone()
        cur.execute("UPDATE transactions SET deleted_at = now() WHERE txn_id = %s", (deleted_txn,))

        cur.execute(
            "SELECT category, SUM(amount) FROM active_transactions"
            " WHERE user_id = %s GROUP BY category",
            (user_id,),
        )
        totals = dict(cur.fetchall())

    assert totals == {food: Decimal("0.60"), groceries: Decimal("30.35")}
    assert all(isinstance(total, Decimal) for total in totals.values())
    conn.rollback()
