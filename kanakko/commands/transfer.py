"""`/transfer` — hand household ownership to another member (§16).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 1/6) —
the smallest, most self-contained command handler, with no dependents outside
`tests/test_transfer.py`. `_command_arg`/`TextMessage` stay in `handlers.py`
(the shared spine every command module needs); importing them here is cheaper
than a second copy.
"""

import time

import psycopg

from kanakko.db import get_or_create_user, household_roster, transfer_ownership
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import TextMessage, _command_arg
from kanakko.tg import send_message

TRANSFER_COMMAND = "/transfer"

TRANSFER_USAGE = (
    "Name the member to hand ownership to — e.g. /transfer ravi. Check "
    "/household for the exact labels."
)
TRANSFER_NO_MATCH = (
    "No member is labelled {label!r}. Check /household for the exact labels."
)
TRANSFER_AMBIGUOUS = (
    "More than one member is labelled {label!r}, so I won't guess which to hand it "
    "to. Give them distinct invite labels first (/invite)."
)
TRANSFER_NOT_OWNER = "Only the household owner can transfer ownership."
TRANSFER_NOT_MEMBER = "That person isn't in your household."
TRANSFER_DONE = (
    "Ownership transferred to {label}. They own the household now — you can leave "
    "it with /remove if you like."
)


def _is_transfer(text: str) -> bool:
    """True when `text` is the `/transfer` command — bare or `/transfer@bot`."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == TRANSFER_COMMAND


def handle_transfer(conn: psycopg.Connection, msg: TextMessage) -> int | None:
    """Hand household ownership to another member — owner only (§16).

    A household always has an owner, so an owner can't leave until they transfer
    first; this is the command that unblocks that `/remove`. `/transfer <label>`
    resolves the label against the sender's own `/household` roster (so it can only
    name a member of their household) and hands ownership over. The owner carries no
    label, so `/transfer` can never name them — a bare `/transfer` is a usage hint,
    not an action. Authorization lives in `transfer_ownership`, which refuses a
    non-owner. Does not commit — the caller owns the transaction. Returns the new
    owner's `user_id` on success, or `None` when refused.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    label = _command_arg(msg.text)
    if not label:
        send_message(msg.chat_id, TRANSFER_USAGE)
        log_event("ownership.transferred", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    matches = [
        member_id
        for member_id, _is_owner, member_label, _name in household_roster(conn, user_id)
        if member_label and member_label.lower() == label.lower()
    ]
    if len(matches) != 1:
        reply = (TRANSFER_NO_MATCH if not matches else TRANSFER_AMBIGUOUS).format(label=label)
        send_message(msg.chat_id, reply)
        log_event("ownership.transferred", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    target_id = matches[0]

    outcome = transfer_ownership(conn, user_id, target_id)
    if outcome != "transferred":
        send_message(msg.chat_id, {
            "not_owner": TRANSFER_NOT_OWNER,
            "not_member": TRANSFER_NOT_MEMBER,
            "already_owner": TRANSFER_NOT_MEMBER,
        }[outcome])
        log_event("ownership.transferred", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start),
                  outcome=outcome)
        return None

    send_message(msg.chat_id, TRANSFER_DONE.format(label=label))
    log_event("ownership.transferred", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return target_id
