"""The runner against a real Postgres, which is the only thing that parses DDL.

`tests/test_migrations.py` reads the `.sql` files as text; this one executes
them. Boots a throwaway cluster in a tmp dir, or uses `$TEST_DATABASE_URL` if
one is set.
"""

import os
import subprocess
from decimal import Decimal
from glob import glob

import psycopg
import pytest

from kanakko.migrate import MIGRATIONS, migrate


def pg_bin(name: str) -> str | None:
    """Debian keeps the server binaries off PATH, under /usr/lib/postgresql."""
    found = glob(f"/usr/lib/postgresql/*/bin/{name}") + glob("/usr/local/pgsql/bin/" + name)
    return sorted(found)[-1] if found else None


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    if dsn := os.environ.get("TEST_DATABASE_URL"):
        with psycopg.connect(dsn) as existing:
            yield existing
        return

    initdb, pg_ctl = pg_bin("initdb"), pg_bin("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no local Postgres server; set TEST_DATABASE_URL to use one")

    data = tmp_path_factory.mktemp("pgdata") / "cluster"
    socket = tmp_path_factory.mktemp("pgsock")
    subprocess.run([initdb, "-D", data, "-U", "postgres", "--auth=trust", "-N"], check=True)
    # No TCP port, so a developer's own Postgres can't be hit by accident.
    subprocess.run(
        [pg_ctl, "-D", data, "-w", "-o", f"-k {socket} -h '' -c fsync=off", "start"], check=True
    )
    try:
        with psycopg.connect(f"postgresql://postgres@/postgres?host={socket}") as fresh:
            yield fresh
    finally:
        subprocess.run([pg_ctl, "-D", data, "-m", "immediate", "stop"], check=True)


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
