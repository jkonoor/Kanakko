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
