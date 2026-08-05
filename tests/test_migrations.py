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


def test_money_is_numeric_12_2_never_float():
    """Every fixed-point column, however named and however introduced.

    Checking the type rather than the column name catches `balance NUMERIC`
    and `ALTER COLUMN amount TYPE NUMERIC(12, 4)` — a wrong scale rounds
    silently on insert, it does not error.
    """
    for sql in MIGRATIONS:
        text = ddl(sql)
        assert not re.search(r"\b(REAL|FLOAT|DOUBLE PRECISION)\b", text, re.I), sql
        for match in re.finditer(r"\bNUMERIC\b\s*(\([^)]*\))?", text, re.I):
            scale = re.sub(r"\s+", "", match.group(1) or "(unspecified)")
            line = text.count("\n", 0, match.start()) + 1
            assert scale == "(12,2)", f"{sql}:{line}: NUMERIC{scale}"


def test_timestamps_are_timezone_aware():
    for sql in MIGRATIONS:
        naive = re.findall(r"\bTIMESTAMP\b(?!\s*(?:TZ|WITH TIME ZONE))", ddl(sql), re.I)
        assert not naive, f"{sql}: naive TIMESTAMP columns"


def test_active_transactions_view_hides_deleted_rows():
    """The soft-delete filter (DECISIONS §6) lives in one place — this view.

    A later `CREATE OR REPLACE VIEW` that drops the WHERE clause resurrects
    every deleted row inside every total, with nothing raising.
    """
    definitions = [
        body
        for sql in MIGRATIONS
        for body in re.findall(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+active_transactions\b(.*?);",
            ddl(sql),
            re.I | re.S,
        )
    ]
    assert definitions, "no migration defines active_transactions"
    assert re.search(r"\bWHERE\s+deleted_at\s+IS\s+NULL", definitions[-1], re.I), (
        "the last definition of active_transactions does not filter deleted rows"
    )
