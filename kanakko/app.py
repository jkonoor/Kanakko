"""FastAPI app: webhook and Mini App routes."""

import hmac
import os
from dataclasses import dataclass

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from kanakko import __version__
from kanakko.confirm import CANCEL, CONFIRM, category_prompt, confirm_card
from kanakko.db import (
    cancel_pending,
    confirm_pending,
    connect,
    get_or_create_user,
    save_pending,
)
from kanakko.parse import parse_message
from kanakko.tg import answer_callback_query, send_message

app = FastAPI(title="Kanakko", version=__version__)

WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _origin_is_verified(request: Request) -> bool:
    """The request carries Telegram's registered `secret_token` (§15).

    Fails closed: an unset `TELEGRAM_WEBHOOK_SECRET` rejects *every* request, so
    a missing secret can never silently mean "accept everything" — the exact
    failure that looks fine in dev and ships an open endpoint. Compared with
    `hmac.compare_digest`, not `==`.
    """
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET") or ""
    if not secret:
        return False
    presented = request.headers.get(WEBHOOK_SECRET_HEADER) or ""
    return hmac.compare_digest(presented, secret)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@dataclass(frozen=True)
class TextMessage:
    """A user typed something — a transaction to parse (§2)."""

    chat_id: int
    message_id: int
    text: str


@dataclass(frozen=True)
class ButtonPress:
    """A user tapped an inline button — Confirm/Cancel/category (§4, §5)."""

    chat_id: int
    message_id: int
    callback_query_id: str
    data: str


def dispatch(update: dict) -> TextMessage | ButtonPress | None:
    """Classify a Telegram update into the one action the core loop acts on.

    A text message becomes a `TextMessage`; an inline-button tap becomes a
    `ButtonPress`. Everything else — edited messages, photos, channel posts,
    bots joining — returns `None` and is ignored. The handlers that consume
    these live in the following tasks (parse→confirm card, Confirm, Cancel,
    category buttons).
    """
    message = update.get("message") or {}
    if isinstance(message.get("text"), str):
        chat = message.get("chat") or {}
        return TextMessage(
            chat_id=chat.get("id"),
            message_id=message.get("message_id"),
            text=message["text"],
        )

    callback = update.get("callback_query") or {}
    if isinstance(callback.get("data"), str):
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        return ButtonPress(
            chat_id=chat.get("id"),
            message_id=msg.get("message_id"),
            callback_query_id=callback.get("id"),
            data=callback["data"],
        )
    return None


REPHRASE_PROMPT = (
    'I couldn\'t find an amount in that. Try again with the amount — '
    'like "spent 500 on groceries" or "got 20000 salary".'
)


def handle_text(conn: psycopg.Connection, msg: TextMessage) -> int | None:
    """Parse a typed message and send its confirm card (§2, §4) — first half of
    the core loop, split off from Handle Confirm.

    Resolve the user, parse the text into a `Transaction`, send the confirm card,
    and store the parsed row keyed by the *sent card's* message id — the id the
    Confirm/Cancel tap carries back (§1). The card is sent *before* the pending
    row is written because that message id doesn't exist until Telegram assigns
    it; a send that fails leaves no pending row, which is the safe direction (a
    dead card the user can retry, never a Confirm with nothing to confirm).

    A message with no parseable amount is no transaction (§3): after `parse_message`
    exhausts its one retry the `ValidationError` propagates here, and instead of
    500ing (which makes Telegram redeliver the same unparseable text forever) we
    ask the user to rephrase and store nothing. Returns `None` in that case.

    A null `category` means the model couldn't tell (§3): we show the category
    picker instead of a confirm card so the user names it in one tap. The pending
    row is still written (keyed by the sent card's id) so the category press can
    update it; the amount, not the category, is what makes it a transaction.

    In a private chat the chat id is the user's Telegram id, so it doubles as the
    `telegram_user_id`. Does not commit — the caller owns the transaction.
    Returns the new `pending_id`, or `None` when the message couldn't be parsed.
    """
    user_id = get_or_create_user(conn, msg.chat_id)
    try:
        txn = parse_message(msg.text)
    except ValidationError:
        send_message(msg.chat_id, REPHRASE_PROMPT)
        return None
    render = category_prompt if txn.category is None else confirm_card
    text, keyboard = render(txn)
    sent = send_message(msg.chat_id, text, reply_markup=keyboard)
    card_message_id = sent["result"]["message_id"]
    return save_pending(conn, user_id, card_message_id, txn)


def handle_confirm(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Confirm the pending transaction the Confirm tap carries, then acknowledge (§4).

    The tap carries the confirm card's message id; `confirm_pending` scopes the
    write to this user (§1) so one user's Confirm can't latch onto another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already stored) — either way we answer the callback query so Telegram
    clears the spinner. Does not commit — the caller owns the transaction.
    Returns the new `txn_id`, or `None` when there was nothing to confirm.
    """
    user_id = get_or_create_user(conn, press.chat_id)
    txn_id = confirm_pending(conn, user_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Saved ✅" if txn_id else "Already saved"
    )
    return txn_id


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
    user_id = get_or_create_user(conn, press.chat_id)
    pending_id = cancel_pending(conn, user_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Discarded ❌" if pending_id else "Already gone"
    )
    return pending_id


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    """Receive a Telegram update and route it (§14).

    Telegram redelivers any update it did not get a 2xx for, so this answers
    200 to *everything* — a malformed body or an update we ignore must not
    become a growing retry loop. But first the origin is verified (§15): a
    request without Telegram's `secret_token` gets a 403 and its body is never
    read, so a forged POST cannot reach `dispatch`.
    """
    if not _origin_is_verified(request):
        raise HTTPException(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    action = dispatch(update if isinstance(update, dict) else {})
    # One connection per handled update, committed on block exit; a redelivered
    # or ignored update opens nothing. Confirm is idempotent (confirm_pending
    # returns None on redelivery), so answering 200 after the commit is safe.
    if isinstance(action, TextMessage):
        with connect() as conn:
            handle_text(conn, action)
    elif isinstance(action, ButtonPress) and action.data == CONFIRM:
        with connect() as conn:
            handle_confirm(conn, action)
    elif isinstance(action, ButtonPress) and action.data == CANCEL:
        with connect() as conn:
            handle_cancel(conn, action)
    return {"ok": True}
