"""Guards over every migration, not just today's.

Money and timezone mistakes in DDL are silent — a `float` amount or a naive
`TIMESTAMP` errors nowhere, it just reports quietly wrong numbers.
"""

import re
from pathlib import Path

MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))


def ddl(sql: Path) -> str:
    """The file's statements without `--` comments, which discuss these types."""
    return re.sub(r"--[^\n]*", "", sql.read_text())


def test_migrations_exist():
    assert MIGRATIONS


def test_amounts_are_numeric_never_float():
    for sql in MIGRATIONS:
        text = ddl(sql)
        assert not re.search(r"\b(REAL|FLOAT|DOUBLE PRECISION)\b", text, re.I), sql
        for column, type_ in re.findall(r"^\s+(\w*amount\w*)\s+(\S+[^,\n]*)", text, re.I | re.M):
            assert type_.upper().startswith("NUMERIC(12, 2)"), f"{sql}: {column} is {type_}"


def test_timestamps_are_timezone_aware():
    for sql in MIGRATIONS:
        naive = re.findall(r"\bTIMESTAMP\b(?!\s*(?:TZ|WITH TIME ZONE))", ddl(sql), re.I)
        assert not naive, f"{sql}: naive TIMESTAMP columns"
