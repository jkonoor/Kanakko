"""Shared fixtures — a throwaway Postgres cluster booted in a tmp dir.

Both the migration runner and the db-layer tests need a real server: DDL and
`NUMERIC(12, 2)` are only promises until one enforces them. The cluster listens
on a unix socket only, never a TCP port, so a developer's own Postgres can't be
hit by accident.
"""

import subprocess
from glob import glob

import psycopg
import pytest

from kanakko.db import create_household_of_one


def household_of(conn, user_id: int) -> int:
    """The user's household id, creating a household-of-one on first call (test-only).

    Production households are created by onboarding (§16), via
    `create_household_of_one` — reused here rather than hand-rolled, so a test
    household is never missing the default `spending`/`external` accounts that
    onboarding always mints. `transactions.account_id` is NOT NULL for every
    non-transfer row (migration 012), so a test-seeded household without a
    default account would fail every `confirm_pending` insert with no household
    to blame. Idempotent — the UNIQUE on `household_members.user_id` makes a
    repeat call a plain lookup, so insert helpers may call it per row.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT household_id FROM household_members WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row is not None:
            return row[0]
    return create_household_of_one(conn, user_id)


def default_account_of(conn, household_id: int) -> int:
    """The household's default `spending` account (test-only, §18).

    `household_of`/`create_household_of_one` always mint one, so this is a plain
    lookup. `transactions.account_id` is NOT NULL for every non-transfer row
    (migration 012), so a raw-SQL transaction insert in these tests needs one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_id FROM accounts"
            " WHERE household_id = %s AND is_default AND deleted_at IS NULL",
            (household_id,),
        )
        return cur.fetchone()[0]


def join_household(conn, household_id: int, user_id: int) -> None:
    """Add an existing user to an existing household (test-only, §16).

    `household_of` makes a household of one; this puts a second member in the same
    one, which the jobs' household-scoping checks need but onboarding would build
    via an invite. The UNIQUE on `household_members.user_id` makes a double-add a
    hard error, which is the intended one-household-per-user rule.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (household_id, user_id),
        )


def pg_bin(name: str) -> str | None:
    """Debian keeps the server binaries off PATH, under /usr/lib/postgresql."""
    found = glob(f"/usr/lib/postgresql/*/bin/{name}") + glob("/usr/local/pgsql/bin/" + name)
    return sorted(found)[-1] if found else None


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    initdb, pg_ctl = pg_bin("initdb"), pg_bin("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no local Postgres server binaries under /usr/lib/postgresql")

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
