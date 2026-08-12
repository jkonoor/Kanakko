"""The reconcile reply — the other half of the weekly nudge (§18).

`jobs.reconcile` sends "I think your Bank has ₹X — what does your bank say?"
and marks it awaiting a reply (`db.create_reconcile_ask`). `app.py`'s webhook
checks `db.pending_awaiting_reconcile` for every `TextMessage`, the same way it
already does for "Change amount", and routes here instead of a fresh parse when
one is outstanding. `handle_reconcile_reply` is the sibling of
`confirm_flow.handle_amount_reply`: no LLM call, no pending row of its own — it
reads the account's own awaiting row and answers it.
"""

import time

import psycopg

from kanakko.db import clear_reconcile_ask, create_adjustment, get_or_create_user
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import TextMessage
from kanakko.jobs.evening import today_ist
from kanakko.money import format_amount, parse_amount
from kanakko.tg import send_message

RECONCILE_RETRY_PROMPT = (
    "I couldn't read that as an amount. Reply with just the number, like 42300."
)
RECONCILE_MATCHED = "Got it — that matches what I already have. No changes needed."
RECONCILE_ADJUSTED = "Logged an adjustment of {amount} to match what you told me."


def handle_reconcile_reply(
    conn: psycopg.Connection, msg: TextMessage, telegram_message_id: int, account_id: int
) -> dict | None:
    """Apply a typed reconcile figure, then answer with what it did (§18).

    An unparseable reply (`money.parse_amount` raising) leaves the ask
    outstanding so the user can just retry — the same contract
    `handle_amount_reply` gives "Change amount". A parseable one clears the ask
    first, then hands the reported figure to `create_adjustment`: a matching
    report writes nothing (`create_adjustment` returns `None`) and gets the
    "no changes needed" reply; a mismatch writes the visible adjustment §18
    requires and the reply names its size. Does not commit — the caller owns
    the transaction. Returns the stored adjustment row, or `None` when the
    reply couldn't be read as an amount or nothing needed reconciling.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    try:
        reported = parse_amount(msg.text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, RECONCILE_RETRY_PROMPT)
        log_event("account.reconciled", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    clear_reconcile_ask(conn, user_id, telegram_message_id)
    row = create_adjustment(
        conn, user_id, account_id, reported, today_ist(),
        source=msg.source, update_id=msg.update_id,
    )
    if row is None:
        send_message(msg.chat_id, RECONCILE_MATCHED)
        log_event("account.reconciled", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start),
                  account_id=account_id)
        return None
    send_message(msg.chat_id, RECONCILE_ADJUSTED.format(amount=format_amount(row["amount"])))
    log_event("account.reconciled", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              account_id=account_id, txn_id=row["txn_id"], amount=row["amount"])
    return row
