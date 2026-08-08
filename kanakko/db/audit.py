"""The `transaction_events` audit write (§17), shared by every money mutation.

`_record_event` runs on the *same cursor* as the money statement it audits, so
the two commit and roll back together — physical adjacency is the only form of
this that cannot come apart (§17).
"""

import json
from functools import partial

import psycopg
from psycopg.types.json import Jsonb

# `default=str` renders the `Decimal` amount and the `date` a ledger row carries;
# JSONB stores them as their canonical strings ("12.50", "2026-08-05"), never a
# float (§9).
_json_dumps = partial(json.dumps, default=str)


def _record_event(
    cur: psycopg.Cursor,
    *,
    txn_id: int,
    user_id: int,
    action: str,
    before: dict | None,
    after: dict | None,
    source: str,
    update_id: int | None,
) -> None:
    """Write one `transaction_events` audit row (§17).

    Called on the *same cursor*, inside each money function's own
    `conn.transaction()` block, so the audit row commits and rolls back with the
    money statement — physical adjacency is the only form of this that cannot come
    apart (§17). `before`/`after` are the row's fields; the side that doesn't
    exist for an action is `None` (a confirm has no before, an undo/delete no
    after).
    """
    cur.execute(
        "INSERT INTO transaction_events"
        " (txn_id, user_id, action, before, after, source, update_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            txn_id,
            user_id,
            action,
            Jsonb(before, dumps=_json_dumps) if before is not None else None,
            Jsonb(after, dumps=_json_dumps) if after is not None else None,
            source,
            update_id,
        ),
    )
