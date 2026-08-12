"""The confirm-card core — every button tap and reply the confirm card takes.

Split out of `kanakko/handlers.py`, whose six-slice command split (see
`kanakko/commands/__init__.py`) got it from 1541 lines down to 728 — still past
CLAUDE.md's 300-line guideline. This is the next seam: `handle_confirm`,
`handle_cancel`, `handle_category`, `handle_account_choice`,
`handle_change_amount_request` and `handle_amount_reply` share no state with
`dispatch`/`handle_start`/`handle_text`/`handle_undo`/`handle_help`/
`handle_household`, which is what stays in `handlers.py` as the spine every
module — this one included — imports `TextMessage`/`ButtonPress` from.
"""

import time

import psycopg

from kanakko.categories import ALL_CATEGORIES, CATEGORY_PREFIX
from kanakko.confirm import ACCOUNT_PREFIX, SKIP_LABEL, confirm_card, settled_card
from kanakko.db import (
    cancel_pending,
    confirm_pending,
    get_or_create_user,
    list_accounts,
    request_amount_change,
    set_pending_account,
    set_pending_amount,
    set_pending_category,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import ButtonPress, TextMessage
from kanakko.money import parse_amount
from kanakko.parse import Transaction
from kanakko.tg import (
    answer_callback_query,
    delete_message,
    edit_message_text,
    send_message,
)

# §18: the "Change amount" reply pair — asked once the button is tapped, and
# again if the reply couldn't be read as an amount. Tapping Skip on the card
# is the way out if the user doesn't want to answer either.
CHANGE_AMOUNT_PROMPT = "Reply with the new amount — just the number, like 5000."
CHANGE_AMOUNT_RETRY_PROMPT = (
    "I couldn't read that as an amount. Reply with just the number, like 5000."
)


def handle_confirm(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Confirm the pending transaction the Confirm tap carries, then acknowledge (§4).

    The tap carries the confirm card's message id; `confirm_pending` scopes the
    write to this user (§1) so one user's Confirm can't latch onto another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already stored) — either way we answer the callback query so Telegram
    clears the spinner. Does not commit — the caller owns the transaction.
    Returns the new `txn_id`, or `None` when there was nothing to confirm.

    On a real confirm the card is edited into a settled receipt with no keyboard
    (§4, §5): the only lasting evidence a transaction was saved is otherwise a
    toast that fades, and dropping the Confirm/Cancel buttons is also what stops a
    later stale Cancel from removing the receipt. A redelivered tap (`row is None`)
    leaves the already-settled card untouched.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    row = confirm_pending(conn, user_id, press.message_id,
                          source=press.source, update_id=press.update_id)
    if row is not None:
        edit_message_text(press.chat_id, press.message_id, settled_card(row))
    answer_callback_query(
        press.callback_query_id, "Saved ✅" if row else "Already saved"
    )
    if row is None:
        log_event("transaction.confirmed", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    log_event("transaction.confirmed", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start),
              txn_id=row["txn_id"], amount=row["amount"])
    return row["txn_id"]


def handle_cancel(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Discard the pending transaction the Cancel tap carries, then acknowledge (§5).

    The tap carries the confirm card's message id; `cancel_pending` scopes the
    delete to this user (§1) so one user's Cancel can't discard another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already gone) — either way we answer the callback query so Telegram
    clears the spinner. Nothing is written to the ledger. Does not commit — the
    caller owns the transaction. Returns the discarded `pending_id`, or `None`
    when there was nothing to cancel.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    pending_id = cancel_pending(conn, user_id, press.message_id)
    # Delete the card *only when this tap actually cancelled a pending row*. A
    # Cancel on an already-confirmed card finds no pending row (Confirm cleared it
    # and settled the card into a receipt), so deleting it would strip the receipt
    # from the chat while the transaction stays in the ledger — the transcript and
    # the ledger would then disagree. The toast still reports what happened, and
    # the user's own message stays — only the bot's live card goes. `delete_message`
    # returns False for a card older than the Bot API's 48-hour window; that just
    # leaves it in place, better than 500ing the tap into a redelivery loop.
    if pending_id is not None:
        delete_message(press.chat_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Discarded ❌" if pending_id else "Already gone"
    )
    log_event("pending.cancelled", status="ok" if pending_id else "noop",
              update_id=press.update_id, source=press.source, user_id=user_id,
              duration_ms=ms_since(start))
    return pending_id


def handle_category(conn: psycopg.Connection, press: ButtonPress) -> Transaction | None:
    """Apply a `cat:<name>` tap to the pending row, then re-render the card (§5).

    Category is the most-often-wrong field, so correcting it is one tap: the tap
    carries the card's message id and the chosen category, `set_pending_category`
    re-writes the pending row (scoped to this user, §1), and we edit the same card
    in place to reflect it — now a full confirm card, so a card that started as
    the null-category picker gains its Confirm/Cancel buttons.

    An unknown category (a forged callback the keyboard never emits) is ignored
    rather than 500ing — a raise here would make Telegram redeliver the bad tap
    forever. A stale card whose pending row is gone answers with a note and edits
    nothing. Does not commit — the caller owns the transaction. Returns the
    updated `Transaction`, or `None` when there was nothing to update.
    """
    start = time.perf_counter()
    category = press.data.removeprefix(CATEGORY_PREFIX)
    if category not in ALL_CATEGORIES:
        answer_callback_query(press.callback_query_id, "Unknown category")
        # A forged tap the keyboard never emits; user left unresolved on purpose,
        # so no `user_id` on this line.
        log_event("pending.recategorised", status="noop", update_id=press.update_id,
                  source=press.source, duration_ms=ms_since(start))
        return None
    user_id = get_or_create_user(conn, press.from_id)
    txn = set_pending_category(conn, user_id, press.message_id, category)
    if txn is None:
        answer_callback_query(press.callback_query_id, "That card's gone")
        log_event("pending.recategorised", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    text, keyboard = confirm_card(txn, list_accounts(conn, user_id))
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Category: {category}")
    log_event("pending.recategorised", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return txn


def handle_account_choice(conn: psycopg.Connection, press: ButtonPress) -> Transaction | None:
    """Apply an `acct:<name>` tap to the pending row, then re-render the card (§18, §5).

    The account picker's counterpart to `handle_category`: the tap carries the
    card's message id and the chosen account name, `set_pending_account`
    re-writes the pending row (scoped to this user, §1), and the same card is
    edited in place to reflect it. Unlike a category (a module-level closed
    set), the valid accounts are per household, so this handler resolves the
    user *first* and checks the name against that household's own
    `list_accounts` — a forged or stale account name is ignored rather than
    500ing, the same non-retry contract `handle_category` uses for a bad
    `cat:` tap. A stale card whose pending row is gone answers with a note and
    edits nothing. Does not commit — the caller owns the transaction. Returns
    the updated `Transaction`, or `None` when there was nothing to update.
    """
    start = time.perf_counter()
    account = press.data.removeprefix(ACCOUNT_PREFIX)
    user_id = get_or_create_user(conn, press.from_id)
    accounts = list_accounts(conn, user_id)
    if account not in accounts:
        answer_callback_query(press.callback_query_id, "Unknown account")
        log_event("pending.reaccounted", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    txn = set_pending_account(conn, user_id, press.message_id, account)
    if txn is None:
        answer_callback_query(press.callback_query_id, "That card's gone")
        log_event("pending.reaccounted", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    text, keyboard = confirm_card(txn, accounts)
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Account: {account}")
    log_event("pending.reaccounted", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return txn


def handle_change_amount_request(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Start "Change amount" on a recurring-rule card: ask for the new figure (§18).

    Marks the pending row `awaiting_amount` (`db.request_amount_change`) so the
    *next* text message this user sends is read as a replacement amount, not a
    new transaction — `app.py`'s webhook checks `pending_awaiting_amount` before
    routing a `TextMessage` anywhere else, which is what makes that distinction
    exist at all. The card itself is left untouched: Confirm and Skip still work
    while a reply is pending, and Skip is the escape hatch if the user changes
    their mind — it deletes the pending row outright, which clears the awaiting
    flag along with it. A stale tap (the card is already gone) is answered and
    ignored rather than 500ing, the same non-retry contract every other button
    handler here uses. Does not commit — the caller owns the transaction.
    Returns the marked `pending_id`, or `None` when there was nothing to mark.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    pending_id = request_amount_change(conn, user_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Send the new amount" if pending_id else "That card's gone"
    )
    if pending_id is not None:
        send_message(press.chat_id, CHANGE_AMOUNT_PROMPT)
    log_event("pending.amount_change_requested", status="ok" if pending_id else "noop",
              update_id=press.update_id, source=press.source, user_id=user_id,
              duration_ms=ms_since(start))
    return pending_id


def handle_amount_reply(
    conn: psycopg.Connection, msg: TextMessage, telegram_message_id: int
) -> Transaction | None:
    """Apply a typed replacement amount, then re-render the card in place (§18).

    The other half of "Change amount": `app.py` resolves `telegram_message_id`
    via `db.pending_awaiting_amount` *before* dispatch, which is what routes
    this message here instead of `handle_text`'s ordinary parse — no LLM call,
    no pending row of its own. An unparseable reply (`money.parse_amount`
    raising) leaves the row still `awaiting_amount` so the user can just try
    again; Skip on the card remains the way out. On success the card is
    re-rendered with the same Skip label and "Change amount" button it had
    before — `set_pending_amount` only changes `amount`, so the category,
    account and note the model or an earlier tap already settled survive
    unchanged. Does not commit — the caller owns the transaction. Returns the
    updated `Transaction`, or `None` when the reply couldn't be read as an
    amount or the card is already gone.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    try:
        amount = parse_amount(msg.text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, CHANGE_AMOUNT_RETRY_PROMPT)
        log_event("pending.amount_changed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    txn = set_pending_amount(conn, user_id, telegram_message_id, amount)
    if txn is None:
        send_message(msg.chat_id, "That card's gone.")
        log_event("pending.amount_changed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    accounts = list_accounts(conn, user_id)
    text, keyboard = confirm_card(
        txn, accounts, cancel_label=SKIP_LABEL, change_amount_button=True
    )
    edit_message_text(msg.chat_id, telegram_message_id, text, reply_markup=keyboard)
    log_event("pending.amount_changed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return txn
