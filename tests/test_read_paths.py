"""Every production read of the ledger goes through `active_transactions` (§6).

`active_transactions` applies the soft-delete filter; a `SELECT ... FROM
transactions` bypasses it and resurrects deleted rows inside totals — silently,
because a wrong total errors nowhere. This guard scans the production modules
for such a read so a future query can't reintroduce the bypass. Writes to the
base table (`INSERT INTO transactions`, `UPDATE transactions SET deleted_at`)
are correct and don't match — a view can't own the soft-delete write.
"""

import re
from pathlib import Path

KANAKKO = Path(__file__).parent.parent / "kanakko"


def sql_only(source: str) -> str:
    """Source with docstrings and `#` comments stripped, keeping the SQL strings.

    Queries live in ordinary `"..."` string literals; the prose that discusses
    `transactions` (e.g. "reading `transactions` directly would...") lives in
    triple-quoted docstrings and `#` comments. Dropping those keeps the guard on
    real queries and off the prose that explains why we avoid them.
    """
    source = re.sub(r'""".*?"""', "", source, flags=re.S)
    source = re.sub(r"'''.*?'''", "", source, flags=re.S)
    source = re.sub(r"#[^\n]*", "", source)
    return source


def test_no_production_read_bypasses_active_transactions():
    r"""No `FROM transactions` in production — the read must go through the view.

    `\btransactions\b` does not match `active_transactions`/`pending_transactions`
    (underscore is a word char, so there's no boundary before `transactions`), so
    only the bare base ledger table trips this.
    """
    offenders = []
    for module in sorted(KANAKKO.glob("*.py")):
        sql = sql_only(module.read_text())
        for match in re.finditer(r"\bfrom\s+transactions\b", sql, re.I):
            line = sql.count("\n", 0, match.start()) + 1
            offenders.append(f"{module.name}:{line}")
    assert not offenders, (
        "reads must go through active_transactions, not transactions: "
        + ", ".join(offenders)
    )
