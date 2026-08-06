"""FastAPI app: webhook and Mini App routes."""

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from kanakko import __version__, configure_logging
from kanakko.categories import ALL_CATEGORIES, CATEGORY_PREFIX
from kanakko.confirm import CANCEL, CONFIRM, category_prompt, confirm_card
from kanakko.db import (
    cancel_pending,
    claim_update,
    confirm_pending,
    connect,
    get_or_create_user,
    month_summary,
    recent_transactions,
    save_pending,
    set_pending_category,
    set_transaction_category,
    soft_delete_transaction,
    totals,
    undo_last,
)
from kanakko.money import format_amount
from kanakko.parse import Transaction, parse_message
from kanakko.tg import answer_callback_query, edit_message_text, send_message
from kanakko.webapp import (
    InitDataError,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    validate_init_data,
    user_id_from_init_data,
    SHELL_HTML,
)

configure_logging()
log = logging.getLogger(__name__)

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


@app.get("/app", response_class=HTMLResponse)
def mini_app_shell() -> str:
    """Serve the Mini App bootstrap page (§13) — no data, no secret, no auth."""
    return SHELL_HTML


TMA_PREFIX = "tma "


@app.get("/app/data", response_class=HTMLResponse)
def mini_app_data(request: Request) -> str:
    """The dashboard fragment for the authenticated user (§13): totals, balance,
    this-week and current-month figures.

    The bootstrap sends `initData` in the `Authorization: tma <initData>` header
    (Telegram's documented scheme). Validation *is* the authentication — a valid
    HMAC proves the payload came from Telegram and names the real user (§13), so
    there is no login. A missing, malformed, or forged payload is a 401; an unset
    `TELEGRAM_BOT_TOKEN` fails closed as a 500 (loud misconfig, not a bypass).
    Read-only, so no `auth_date` freshness check is needed yet — the replay
    concern arrives with the per-row mutations of tasks 100/101 (see REVIEWS.md).
    """
    header = request.headers.get("Authorization") or ""
    if not header.startswith(TMA_PREFIX):
        raise HTTPException(status_code=401)
    try:
        fields = validate_init_data(header[len(TMA_PREFIX):])
        telegram_user_id = user_id_from_init_data(fields)
    except InitDataError:
        raise HTTPException(status_code=401)

    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        income, expenses = totals(conn, user_id)
        first, next_first = current_month_ist()
        m_income, m_expenses, top = month_summary(conn, user_id, first, next_first)
        w_first, w_next = current_week_ist()
        # month_summary is a generic date-range summary; its top categories are
        # the month's, so the week's are discarded — the week section is figures only.
        w_income, w_expenses, _ = month_summary(conn, user_id, w_first, w_next)
        recent = recent_transactions(conn, user_id)
    return dashboard_html(
        income, expenses, w_income, w_expenses,
        first.strftime("%B %Y"), m_income, m_expenses, top, recent,
    )


@app.post("/app/delete")
async def mini_app_delete(request: Request) -> Response:
    """Soft-delete one of the authenticated user's transactions (§13, task 100).

    The dashboard's per-row delete button POSTs `{"id": <txn_id>}` here with the
    same `Authorization: tma <initData>` header the read route uses. Unlike
    `/app/data` this route *mutates state*, so it passes
    `max_age=timedelta(hours=24)`: a captured `initData` must not stay a working
    delete button forever (§13, and the official SDK's 24h default). The row is
    scoped to the user resolved from the *signed* `user` object, so one user
    cannot delete another's transaction by guessing an id.

    A missing/forged/stale payload is 401; a body without a usable integer `id`
    is 400; a delete that matched no live row of this user's is 404 (already gone
    or never theirs). Success is 204 — the client re-fetches `/app/data`.
    """
    header = request.headers.get("Authorization") or ""
    if not header.startswith(TMA_PREFIX):
        raise HTTPException(status_code=401)
    try:
        fields = validate_init_data(
            header[len(TMA_PREFIX):], max_age=timedelta(hours=24)
        )
        telegram_user_id = user_id_from_init_data(fields)
    except InitDataError:
        raise HTTPException(status_code=401)

    try:
        body = await request.json()
        txn_id = int(body["id"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        deleted = soft_delete_transaction(conn, user_id, txn_id)
    if deleted is None:
        raise HTTPException(status_code=404)
    return Response(status_code=204)


@app.post("/app/category")
async def mini_app_category(request: Request) -> Response:
    """Change the category of one of the user's transactions (§5, §13, task 101).

    The dashboard's per-row category `<select>` POSTs
    `{"id": <txn_id>, "category": <name>}` here with the same
    `Authorization: tma <initData>` header the read route uses. Like `/app/delete`
    this *mutates state*, so it passes `max_age=timedelta(hours=24)` — a captured
    `initData` must not stay a working edit button forever (§13). `category` must
    be one of the closed set (`categories.py`, §11); anything else is a 400, so a
    forged body can't write a junk label. The row is scoped to the user resolved
    from the *signed* `user` object, so one user cannot relabel another's row.

    A missing/forged/stale payload is 401; a body without a usable integer `id` or
    with an unknown `category` is 400; an id matching no live row of this user's is
    404. Success is 204 — the client re-fetches `/app/data`.
    """
    header = request.headers.get("Authorization") or ""
    if not header.startswith(TMA_PREFIX):
        raise HTTPException(status_code=401)
    try:
        fields = validate_init_data(
            header[len(TMA_PREFIX):], max_age=timedelta(hours=24)
        )
        telegram_user_id = user_id_from_init_data(fields)
    except InitDataError:
        raise HTTPException(status_code=401)

    try:
        body = await request.json()
        txn_id = int(body["id"])
        category = body["category"]
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)
    if category not in ALL_CATEGORIES:
        raise HTTPException(status_code=400)

    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        updated = set_transaction_category(conn, user_id, txn_id, category)
    if updated is None:
        raise HTTPException(status_code=404)
    return Response(status_code=204)


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
    user_id = get_or_create_user(conn, msg.chat_id)
    removed = undo_last(conn, user_id)
    if removed is None:
        send_message(msg.chat_id, "Nothing to undo.")
        return None
    line = f"Removed: {removed['type'].capitalize()} — {format_amount(removed['amount'])}"
    if removed["category"]:
        line += f" ({removed['category']})"
    send_message(msg.chat_id, line)
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
    category = press.data.removeprefix(CATEGORY_PREFIX)
    if category not in ALL_CATEGORIES:
        answer_callback_query(press.callback_query_id, "Unknown category")
        return None
    user_id = get_or_create_user(conn, press.chat_id)
    txn = set_pending_category(conn, user_id, press.message_id, category)
    if txn is None:
        answer_callback_query(press.callback_query_id, "That card's gone")
        return None
    text, keyboard = confirm_card(txn)
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Category: {category}")
    return txn


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    """Receive a Telegram update and route it (§14).

    Telegram redelivers any update it did not get a 2xx for, so this answers
    200 to *everything* — a malformed body or an update we ignore must not
    become a growing retry loop. But first the origin is verified (§15): a
    request without Telegram's `secret_token` gets a 403 and its body is never
    read, so a forged POST cannot reach `dispatch`.

    A redelivery carries the same `update_id`; `claim_update` records it in the
    handler's own transaction and returns False on a repeat, so the handler runs
    at most once per update. That is what keeps `/undo` from soft-deleting a
    *second* real transaction on a redelivery — it has no per-message anchor the
    way Confirm/Cancel do — and hardens every other handler for free.
    """
    if not _origin_is_verified(request):
        raise HTTPException(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    if not isinstance(update, dict):
        return {"ok": True}

    action = dispatch(update)
    if action is None:
        return {"ok": True}

    # One connection per handled update, committed on block exit. The claim and
    # the handler's writes share that transaction, so a redelivery that arrives
    # before the first commit blocks on the id and then finds it taken; a handler
    # that 500s rolls the claim back and the redelivery legitimately re-runs.
    with connect() as conn:
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not claim_update(conn, update_id):
            return {"ok": True}  # a prior delivery of this update was handled
        if isinstance(action, TextMessage):
            if _is_undo(action.text):
                handle_undo(conn, action)
            else:
                handle_text(conn, action)
        elif action.data == CONFIRM:
            handle_confirm(conn, action)
        elif action.data == CANCEL:
            handle_cancel(conn, action)
        elif action.data.startswith(CATEGORY_PREFIX):
            handle_category(conn, action)
    log.info("handled update %s: %s", update.get("update_id"), type(action).__name__)
    return {"ok": True}
