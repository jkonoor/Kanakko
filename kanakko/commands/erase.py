"""`/delete_account` — the way out of the product, not just the household (§16).

`/remove` leaves a *household* and re-homes you into a fresh one of your own.
That is not an exit, and for the sole member of a household it was not even
possible: they own it, an owner must transfer first, and there is nobody to
transfer to. This is the exit — and it is the only irreversible thing a user can
do to themselves, so it asks twice.
"""

import time

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.db import delete_account, find_user
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import ButtonPress, TextMessage
from kanakko.tg import answer_callback_query, edit_message_text, send_message

ERASE_COMMAND = "/delete_account"

# One button, its own prefix. Confirm is a tap rather than a typed phrase: the
# card carries the warning right above it, which a typed "DELETE" does not.
ERASE_PREFIX = "erase:"
ERASE_CONFIRM = "yes"

ERASE_WARNING = (
    "⚠️ Delete your account?\n\n"
    "This erases every entry you've logged, your accounts and balances, and any "
    "recurring rules — permanently. It cannot be undone, and /undo won't bring "
    "any of it back.\n\n"
    "If you're the only one in your household, the household goes too. If others "
    "are in it, they keep theirs.\n\n"
    "Tap below to confirm, or just ignore this message."
)

ERASE_OWNER_MUST_TRANSFER = (
    "You own a household other people are in, so I can't erase you out from "
    "under them. Hand ownership over first with /transfer <name>, then try again."
)

ERASE_DONE = (
    "Your account is gone. Every entry, account and rule has been deleted, and "
    "I no longer know who you are.\n\n"
    "If you ever want to come back you'll need a fresh invite — this is a clean "
    "slate, not a suspended account."
)

ERASE_UNKNOWN = "There's no account here to delete."


def _is_erase(text: str) -> bool:
    """True when `text` is `/delete_account` — bare or `@bot`-suffixed."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == ERASE_COMMAND


def _erase_keyboard() -> InlineKeyboardMarkup:
    """The single confirm button. No "Cancel": ignoring the card is the cancel,
    and a Cancel button beside a destructive one is a mis-tap waiting to happen."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "Delete everything", callback_data=f"{ERASE_PREFIX}{ERASE_CONFIRM}")]]
    )


def handle_erase(conn: psycopg.Connection, msg: TextMessage) -> bool:
    """Show the erasure warning and its confirm button (§16).

    Only ever *asks*. `handle_erase_choice` is what acts, so the typed command
    alone can never destroy anything — the same shape `/remove` uses for its
    retain/delete question, and for the same reason. Resolves with `find_user`
    rather than `get_or_create_user`: minting a row for someone asking to be
    erased would be absurd. Does not commit — the caller owns the transaction.
    Returns True when the question was asked.
    """
    start = time.perf_counter()
    user_id = find_user(conn, msg.from_id)
    if user_id is None:
        send_message(msg.chat_id, ERASE_UNKNOWN)
        log_event("account.erased", status="noop", update_id=msg.update_id,
                  source=msg.source, duration_ms=ms_since(start), outcome="unknown")
        return False
    send_message(msg.chat_id, ERASE_WARNING, reply_markup=_erase_keyboard())
    log_event("account.erased", status="noop", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              outcome="asked")
    return True


def handle_erase_choice(conn: psycopg.Connection, press: ButtonPress) -> str | None:
    """Act on the confirm button — this is the one that erases (§16).

    The button is untrusted, so authorization is re-derived from the *presser*
    (`from_id`) and never from the callback data, which carries no user id at
    all for exactly that reason. A presser whose row is already gone — a double
    tap on the same card — resolves to `None` and is answered as expired rather
    than raising. `delete_account` refuses an owner whose household still has
    members, the same §16 rule `/remove` applies.

    The card is edited into a settled state so a stale button can't be tapped
    again, and because the transcript should record what happened. Does not
    commit — the caller owns the transaction. Returns the outcome, or `None`.
    """
    start = time.perf_counter()
    user_id = find_user(conn, press.from_id)
    if user_id is None:
        answer_callback_query(press.callback_query_id, "That button's expired")
        log_event("account.erased", status="noop", update_id=press.update_id,
                  source=press.source, duration_ms=ms_since(start), outcome="expired")
        return None

    outcome = delete_account(conn, user_id)
    if outcome != "ok":
        answer_callback_query(press.callback_query_id, "Transfer ownership first")
        edit_message_text(press.chat_id, press.message_id, ERASE_OWNER_MUST_TRANSFER)
        log_event("account.erased", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id,
                  duration_ms=ms_since(start), outcome=outcome)
        return outcome

    answer_callback_query(press.callback_query_id, "Deleted")
    edit_message_text(press.chat_id, press.message_id, ERASE_DONE)
    log_event("account.erased", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return outcome
