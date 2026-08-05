"""Applies `migrations/*.sql` in filename order, once each.

Run it with `uv run python -m kanakko.migrate`; the compose stack runs it on
web start. Every applied file is recorded in `schema_migrations`, which this
module creates — it cannot live in a migration, it has to exist first.
"""

import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).parent.parent / "migrations"

# Any 64-bit constant; it only has to be the same in every process that
# migrates, so web and cron booting together queue instead of colliding.
LOCK_KEY = 8_112_025_001


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every unapplied migration. Returns the filenames that ran.

    One transaction for the whole run — Postgres DDL is transactional, so a
    syntax error in file 3 leaves files 1 and 2 unapplied rather than half a
    schema and a bookkeeping table that disagrees with it.
    """
    applied = []
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY,"
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        done = {filename for (filename,) in cur.fetchall()}
        for sql in sorted(MIGRATIONS.glob("*.sql")):
            if sql.name in done:
                continue
            cur.execute(sql.read_text())
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (sql.name,))
            applied.append(sql.name)
    return applied


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL is not set")
    with psycopg.connect(dsn) as conn:
        applied = migrate(conn)
    print("\n".join(f"applied {name}" for name in applied) or "nothing to apply")


if __name__ == "__main__":
    main()
