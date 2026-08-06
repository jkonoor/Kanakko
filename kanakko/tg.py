"""Thin Telegram Bot API send client (§4, §14).

The counterpart to `parse.py`'s OpenRouter call: raw `httpx` against the Bot
API, token from the environment, no python-telegram-bot `Bot`/`Application`
runtime. The Confirm/Cancel/category handlers use these three methods to
acknowledge a tap and edit or send the confirm card. `confirm_card` already
returns a python-telegram-bot `InlineKeyboardMarkup`, so `reply_markup` accepts
that object and serialises it to the Bot API's JSON shape via `.to_dict()`.

Fails closed on an unset `TELEGRAM_BOT_TOKEN` (a `RuntimeError` before any
network I/O), same as `parse.call()` does for `OPENROUTER_API_KEY` — the secret
comes from the environment, never a literal.
"""

import os

import httpx
from telegram import InlineKeyboardMarkup

API_BASE = "https://api.telegram.org"


def _call(method: str, payload: dict) -> dict:
    """POST `payload` to Bot API `method` and return the decoded JSON.

    Raises `RuntimeError` if `TELEGRAM_BOT_TOKEN` is unset; network and HTTP
    errors propagate as `httpx` exceptions.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    # ponytail: sync httpx like parse.call(); a single-user bot's send blocks
    # the loop for one round-trip. Rate-limited async send loop at ~50k users
    # (DECISIONS §14 deferred table), not before.
    response = httpx.post(
        f"{API_BASE}/bot{token}/{method}",
        json=payload,
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def answer_callback_query(callback_query_id: str, text: str | None = None) -> dict:
    """Acknowledge an inline-button tap so Telegram clears the loading spinner."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    return _call("answerCallbackQuery", payload)


def send_message(
    chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> dict:
    """Send `text` to `chat_id`, optionally with an inline keyboard."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup.to_dict()
    return _call("sendMessage", payload)


def edit_message_text(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> dict:
    """Replace the text (and keyboard) of an already-sent message.

    A re-render identical to what the message already shows (e.g. re-tapping the
    already-selected category on a confirm card) is a Bot API 400 "message is not
    modified". The message already reads the way we wanted, so that is success,
    not an error — swallow it rather than let it raise into a 500 that Telegram
    would answer by redelivering the same tap forever.
    """
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup.to_dict()
    try:
        return _call("editMessageText", payload)
    except httpx.HTTPStatusError as exc:
        if _is_not_modified(exc):
            return exc.response.json()
        raise


def _is_not_modified(exc: httpx.HTTPStatusError) -> bool:
    """The edit was a no-op — Bot API 400 "message is not modified"."""
    if exc.response.status_code != 400:
        return False
    try:
        description = exc.response.json().get("description", "")
    except ValueError:
        return False
    return "message is not modified" in description
