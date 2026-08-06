"""FastAPI app: webhook and Mini App routes."""

import hmac
import logging
import os
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request

from kanakko import __version__

app = FastAPI(title="Kanakko", version=__version__)
log = logging.getLogger(__name__)

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
    if action is not None:
        # ponytail: handling (parse→confirm card, Confirm/Cancel, category
        # buttons) is tasks 42–46; this endpoint receives and routes only.
        log.info("dispatched %s", type(action).__name__)
    return {"ok": True}
