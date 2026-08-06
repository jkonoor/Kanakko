"""Every production read of the ledger goes through `active_transactions` (§6).

`active_transactions` applies the soft-delete filter; a `SELECT ... FROM
transactions` bypasses it and resurrects deleted rows inside totals — silently,
because a wrong total errors nowhere. This guard scans the production modules
for such a read so a future query can't reintroduce the bypass. Writes to the
base table (`INSERT INTO transactions`, `UPDATE transactions SET deleted_at`)
are correct and don't match — a view can't own the soft-delete write.
"""

import ast
import re
from pathlib import Path

KANAKKO = Path(__file__).parent.parent / "kanakko"

# `from`/`join` because both read rows: `FROM active_transactions a JOIN
# transactions t` pulls soft-deleted `t` rows into the row set just as a bare
# `FROM transactions` does. `\btransactions\b` won't match `active_transactions`
# or `pending_transactions` — the underscore is a word char, so there's no
# boundary before `transactions`, and only the bare base ledger table trips it.
BYPASS = re.compile(r"\b(from|join)\s+transactions\b", re.I)


def sql_literals(source: str):
    """(value, lineno) for every string literal that isn't a docstring.

    SQL lives in ordinary string literals — single-line `"..."` or multi-line
    `\"\"\"...\"\"\"`; the prose that discusses `transactions` lives in
    docstrings and `#` comments. Parsing with `ast` scans every real string
    (including triple-quoted SQL, the idiomatic way to write a multi-line
    query) while skipping the docstrings that only explain the invariant.
    Comments aren't string nodes, so they're excluded for free.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.value, node.lineno


def test_no_production_read_bypasses_active_transactions():
    r"""No `FROM`/`JOIN transactions` in production — reads go through the view.

    `\btransactions\b` does not match `active_transactions`/`pending_transactions`
    (underscore is a word char, so there's no boundary before `transactions`), so
    only the bare base ledger table trips this.
    """
    offenders = []
    for module in sorted(KANAKKO.rglob("*.py")):
        for value, lineno in sql_literals(module.read_text()):
            if BYPASS.search(value):
                offenders.append(f"{module.name}:{lineno}")
    assert not offenders, (
        "reads must go through active_transactions, not transactions: "
        + ", ".join(offenders)
    )
