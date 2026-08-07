"""What to do with a Telegram update — the model, the dispatch, the handlers.

Split out of `app.py`, which had grown to hold three jobs at once: the HTTP
surface, this update layer, and the Mini App routes. `app.py` keeps the routes
and owns the transaction; everything here is transport-agnostic and takes an open
connection, which is what makes it testable against Postgres without a webhook.

Every handler leaves the commit to its caller (§14), and none of them stores
anything on a path that failed.
"""

import logging
import time
from dataclasses import dataclass

import httpx
import psycopg
from pydantic import ValidationError

from kanakko.categories import ALL_CATEGORIES, CATEGORY_PREFIX
from kanakko.confirm import category_prompt, confirm_card
from kanakko.db import (
    cancel_pending,
    confirm_pending,
    get_or_create_user,
    save_pending,
    set_pending_category,
    undo_last,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.money import format_amount
from kanakko.parse import Transaction, build_request, parse_message, resolve_model
from kanakko.tg import (
    answer_callback_query,
    delete_message,
    edit_message_text,
    send_message,
)
from kanakko.trace import open_trace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextMessage:
    """A user typed something — a transaction to parse (§2).

    `from_id` is *who sent it* (Telegram `message.from.id`), the identity every
    handler resolves the user from (§16); `chat_id` is only the send target.
    They coincide in a private chat, so the two split apart the moment a
    household or a group exists. `from_id` falls back to `chat_id` when unset —
    the private-chat truth — which keeps the many test sites that predate the
    split valid.

    `update_id`/`source` are the §17 correlation fields, set by `dispatch`: the
    `update_id` is what ties every event of one Telegram delivery together, and
    `source` is always `webhook` here. They default so the many test
    construction sites that predate §17 stay valid.
    """

    chat_id: int
    message_id: int
    text: str
    from_id: int | None = None
    update_id: int | None = None
    source: str = "webhook"

    def __post_init__(self) -> None:
        if self.from_id is None:
            object.__setattr__(self, "from_id", self.chat_id)


@dataclass(frozen=True)
class ButtonPress:
    """A user tapped an inline button — Confirm/Cancel/category (§4, §5).

    `from_id` is the sender's identity, `chat_id` the send target — see
    `TextMessage`. `update_id`/`source`: the §17 correlation fields.
    """

    chat_id: int
    message_id: int
    callback_query_id: str
    data: str
    from_id: int | None = None
    update_id: int | None = None
    source: str = "webhook"

    def __post_init__(self) -> None:
        if self.from_id is None:
            object.__setattr__(self, "from_id", self.chat_id)


def dispatch(update: dict) -> TextMessage | ButtonPress | None:
    """Classify a Telegram update into the one action the core loop acts on.

    A text message becomes a `TextMessage`; an inline-button tap becomes a
    `ButtonPress`. Everything else — edited messages, photos, channel posts,
    bots joining — returns `None` and is ignored. The handlers that consume
    these live in the following tasks (parse→confirm card, Confirm, Cancel,
    category buttons).
    """
    update_id = update.get("update_id")
    message = update.get("message") or {}
    if isinstance(message.get("text"), str):
        chat = message.get("chat") or {}
        return TextMessage(
            chat_id=chat.get("id"),
            message_id=message.get("message_id"),
            text=message["text"],
            from_id=(message.get("from") or {}).get("id"),
            update_id=update_id,
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
            from_id=(callback.get("from") or {}).get("id"),
            update_id=update_id,
        )
    return None


REPHRASE_PROMPT = (
    'I couldn\'t find an amount in that. Try again with the amount — '
    'like "spent 500 on groceries" or "got 20000 salary".'
)

PARSER_DOWN_PROMPT = (
    "I can't reach my parser right now — your message wasn't saved. "
    "Please try again shortly."
)

ACCESS_REFUSED = (
    "This bot is invite-only right now. Ask whoever told you about it for an "
    "invite link to get started."
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

    A **4xx** from OpenRouter is permanent (§6): a bad key (401), exhausted credits
    (402), a rejected schema (400) — no amount of Telegram redelivery fixes it. We
    tell the user the parser is unreachable, store nothing, and return `None` so the
    webhook answers 200 and the retry loop stops. A **5xx** or network/timeout error
    is transient, so it propagates: the webhook 500s and Telegram's redelivery is the
    recovery (mirrors the Handle Confirm contract). Store nothing either way. Either
    upstream failure is logged at WARNING with the status and provider body so the
    next occurrence is diagnosable from the container log (the API key rides in the
    request headers, not the body, so it can't leak into the line).

    A null `category` means the model couldn't tell (§3): we show the category
    picker instead of a confirm card so the user names it in one tap. The pending
    row is still written (keyed by the sent card's id) so the category press can
    update it; the amount, not the category, is what makes it a transaction.

    The user is resolved from `from_id` (the sender), never `chat_id` (the send
    target) — they coincide in a private chat but split under a household or group
    (§16). Does not commit — the caller owns the transaction.
    Returns the new `pending_id`, or `None` when the message couldn't be parsed.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    # Trace mode (§17): the raw text, the prompt built from it, and the outcome —
    # written to a per-update folder so a hard parse bug is diagnosable. On by
    # default, a no-op when disabled or unconfigured, and it never raises.
    tr = open_trace(msg.update_id)
    tr.write("input", {"text": msg.text, "user_id": user_id,
                       "update_id": msg.update_id, "source": msg.source})
    tr.write("request", build_request(msg.text))
    parse_start = time.perf_counter()
    try:
        txn = parse_message(msg.text)
    except ValidationError as exc:
        tr.write("parse", {"error": str(exc)}, outcome="invalid")
        send_message(msg.chat_id, REPHRASE_PROMPT)
        return None
    except httpx.HTTPStatusError as exc:
        # The API key rides in the request headers, not the response body, so
        # logging status + body can't leak it (asserted in the check).
        log.warning(
            "parse upstream failure: status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        tr.write("parse", {"status": exc.response.status_code,
                           "body": exc.response.text},
                 outcome=f"upstream_{exc.response.status_code}")
        if not 400 <= exc.response.status_code < 500:
            raise  # 5xx is transient — let it 500 so Telegram redelivers
        send_message(msg.chat_id, PARSER_DOWN_PROMPT)
        return None
    tr.write("parse", txn.model_dump(), outcome="ok")
    # §17: the success side of the parse — Phase 6's WARNING covers the failure.
    # `duration_ms` is the call alone (retry included) so a slow model is visible
    # before it reads as the bot feeling sluggish.
    log_event("parse.completed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id,
              duration_ms=ms_since(parse_start), model=resolve_model())
    render = category_prompt if txn.category is None else confirm_card
    text, keyboard = render(txn)
    sent = send_message(msg.chat_id, text, reply_markup=keyboard)
    card_message_id = sent["result"]["message_id"]
    pending_id = save_pending(conn, user_id, card_message_id, txn)
    log_event("pending.created", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return pending_id


UNDO_COMMAND = "/undo"


def _is_undo(text: str) -> bool:
    """True when `text` is the `/undo` command — bare or `/undo@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == UNDO_COMMAND


def handle_undo(conn: psycopg.Connection, msg: TextMessage) -> dict | None:
    """Soft-delete the user's most recent confirmed transaction and confirm it (§5, §6).

    `/undo` is the correction path for a just-confirmed entry: `undo_last` sets
    `deleted_at` on the newest live row (recoverable, §6) and returns its fields
    so the reply names exactly what was removed. Nothing to undo — a fresh user,
    or a `/undo` past the last row — gets a plain "Nothing to undo." Does not
    commit — the caller owns the transaction. Returns the removed row, or `None`.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    removed = undo_last(conn, user_id, source=msg.source, update_id=msg.update_id)
    if removed is None:
        send_message(msg.chat_id, "Nothing to undo.")
        log_event("transaction.undone", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    line = f"Removed: {removed['type'].capitalize()} — {format_amount(removed['amount'])}"
    if removed["category"]:
        line += f" ({removed['category']})"
    send_message(msg.chat_id, line)
    log_event("transaction.undone", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              txn_id=removed["txn_id"], amount=removed["amount"])
    return removed


def handle_confirm(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Confirm the pending transaction the Confirm tap carries, then acknowledge (§4).

    The tap carries the confirm card's message id; `confirm_pending` scopes the
    write to this user (§1) so one user's Confirm can't latch onto another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already stored) — either way we answer the callback query so Telegram
    clears the spinner. Does not commit — the caller owns the transaction.
    Returns the new `txn_id`, or `None` when there was nothing to confirm.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    row = confirm_pending(conn, user_id, press.message_id,
                          source=press.source, update_id=press.update_id)
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
    # Take the cancelled card out of the chat rather than leaving a dead card
    # with live buttons. The toast still reports what happened, and the user's
    # own message stays — only the bot's card goes. `delete_message` returns
    # False for a card older than the Bot API's 48-hour window; that just leaves
    # it in place, which is better than 500ing the tap into a redelivery loop.
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
    text, keyboard = confirm_card(txn)
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Category: {category}")
    log_event("pending.recategorised", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return txn
