"""FastAPI app: webhook, Mini App shell, and health check.

The Mini App's data routes (`/app/data`, `/app/delete`, `/app/category`,
`/app/edit`) live in `kanakko.webapp.routes` — this file was 497 lines once the
edit route (task 974) landed, past CLAUDE.md's 300-line guideline, and those
four routes plus `authenticated_user`/`permitted_user` were the obvious,
self-contained seam to move. `/app/refund` (task 1082) and `/app/recurring/*`
(task 1161 split 2/2a) are sibling modules for the same reason — `routes.py`
was already at the 300-line guideline, so a fifth (then seventh) route there
would have overshot it again.
"""

import hmac
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from kanakko import __version__, configure_logging
from kanakko.auth import is_authorized, within_daily_cap
from kanakko.categories import CATEGORY_PREFIX
from kanakko.commands.account import _is_account, handle_account
from kanakko.commands.invite import (
    _is_invite,
    _is_invite_signup,
    handle_invite,
    handle_invite_signup,
)
from kanakko.commands.recurring import _is_recurring, handle_recurring
from kanakko.commands.refund import REFUND_PREFIX, _is_refund, handle_refund, handle_refund_choice
from kanakko.commands.remove import REMOVE_PREFIX, _is_remove, handle_remove, handle_remove_choice
from kanakko.commands.transfer import _is_transfer, handle_transfer
from kanakko.confirm import ACCOUNT_PREFIX, CANCEL, CHANGE_AMOUNT, CONFIRM
from kanakko.confirm_flow import (
    handle_account_choice,
    handle_amount_reply,
    handle_cancel,
    handle_category,
    handle_change_amount_request,
    handle_confirm,
)
from kanakko.db import claim_update, connect, get_or_create_user, pending_awaiting_amount
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import (
    ACCESS_REFUSED,
    CAP_REACHED,
    TextMessage,
    _is_greeting,
    _is_help,
    _is_household,
    _is_start,
    _is_undo,
    dispatch,
    handle_help,
    handle_household,
    handle_start,
    handle_text,
    handle_undo,
)
from kanakko.tg import send_message
from kanakko.webapp import SHELL_HTML
from kanakko.webapp.recurring import router as webapp_recurring_router
from kanakko.webapp.refund import router as webapp_refund_router
from kanakko.webapp.routes import router as webapp_router

configure_logging()

app = FastAPI(title="Kanakko", version=__version__)
app.include_router(webapp_router)
app.include_router(webapp_refund_router)
app.include_router(webapp_recurring_router)

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

    The dispatch is wrapped once so the error side of §17 is logged in one place,
    not in seven handlers: a handler that raises produces exactly one
    `update.handled` line with `status="error"` and then re-raises, keeping the
    §14 500-and-redeliver contract. The per-handler events only ever report `ok`
    or `noop`.
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

    # /start is onboarding and the one path that must run *before* the gate:
    # consuming an invite is how an unknown user becomes authorized (§16), so the
    # gate cannot precede it. It makes no LLM call and carries no per-message
    # anchor, so it is neither metered nor claimed — `consume_invite` is
    # idempotent on a redelivery (a code this same user already used returns
    # success, not a refusal), which is what a `claim_update` would otherwise buy.
    if isinstance(action, TextMessage) and _is_start(action.text):
        with connect() as conn:
            handle_start(conn, action)
        return {"ok": True}

    # One connection per handled update, committed on block exit. The claim and
    # the handler's writes share that transaction, so a redelivery that arrives
    # before the first commit blocks on the id and then finds it taken; a handler
    # that 500s rolls the claim back and the redelivery legitimately re-runs.
    start = time.perf_counter()
    update_id = update.get("update_id")
    with connect() as conn:
        # §16: is this user permitted at all? — the one check before any LLM call
        # (§2 costs money). An unrecognised user in invite mode is refused here and
        # nothing is stored: the gate reads with `user_exists`, so the update is
        # turned away before `claim_update` or any handler mints a row. `open` mode
        # admits everyone; `invite` mode admits only users who already have a row.
        if not is_authorized(conn, action.from_id):
            send_message(action.chat_id, ACCESS_REFUSED)
            log_event("update.refused", status="noop", update_id=update_id,
                      source="webhook", duration_ms=ms_since(start))
            return {"ok": True}
        user_id = get_or_create_user(conn, action.from_id)
        # §18: a "Change amount" tap marks its card `awaiting_amount` (below), so
        # the *next* text message this user sends is a replacement amount, not a
        # transaction to parse — checked once, up front, for every TextMessage.
        awaiting_message_id = (
            pending_awaiting_amount(conn, user_id) if isinstance(action, TextMessage) else None
        )
        # §16 cost control: only the text-parse path is an LLM call (§2), so a
        # runaway user is an unbounded bill on the owner's OpenRouter credits.
        # /undo and the Confirm/Cancel/category taps cost nothing — they are never
        # capped (a user at the cap can still finish or undo a pending card) and,
        # crucially, never *metered*: the claim below stamps `user_id` only on the
        # parse path, leaving callback/undo rows NULL so `count_updates_on_day`
        # counts exactly the messages that spend credits (§16), not the free taps.
        # An amount reply is the same kind of free tap — no LLM call — so it is
        # excluded here too.
        is_parse = isinstance(action, TextMessage) and awaiting_message_id is None and not (
            _is_undo(action.text)
            or _is_help(action.text)
            or _is_greeting(action.text)
            or _is_invite(action.text)
            or _is_invite_signup(action.text)
            or _is_household(action.text)
            or _is_remove(action.text)
            or _is_transfer(action.text)
            or _is_account(action.text)
            or _is_recurring(action.text)
            or _is_refund(action.text)
        )
        if is_parse and not within_daily_cap(conn, user_id):
            send_message(action.chat_id, CAP_REACHED)
            log_event("update.capped", status="noop", update_id=update_id,
                      source="webhook", user_id=user_id, duration_ms=ms_since(start))
            return {"ok": True}
        # Meter only the parse path (see above). Counted off the yet-unclaimed
        # count, so the (cap+1)th message is the one refused.
        metered_user = user_id if is_parse else None
        if isinstance(update_id, int) and not claim_update(conn, update_id, metered_user):
            return {"ok": True}  # a prior delivery of this update was handled
        try:
            if isinstance(action, TextMessage):
                if awaiting_message_id is not None:
                    handle_amount_reply(conn, action, awaiting_message_id)
                elif _is_undo(action.text):
                    handle_undo(conn, action)
                elif _is_help(action.text) or _is_greeting(action.text):
                    handle_help(conn, action)
                elif _is_invite_signup(action.text):
                    handle_invite_signup(conn, action)
                elif _is_invite(action.text):
                    handle_invite(conn, action)
                elif _is_household(action.text):
                    handle_household(conn, action)
                elif _is_remove(action.text):
                    handle_remove(conn, action)
                elif _is_transfer(action.text):
                    handle_transfer(conn, action)
                elif _is_account(action.text):
                    handle_account(conn, action)
                elif _is_recurring(action.text):
                    handle_recurring(conn, action)
                elif _is_refund(action.text):
                    handle_refund(conn, action)
                else:
                    handle_text(conn, action)
            elif action.data == CONFIRM:
                handle_confirm(conn, action)
            elif action.data == CANCEL:
                handle_cancel(conn, action)
            elif action.data == CHANGE_AMOUNT:
                handle_change_amount_request(conn, action)
            elif action.data.startswith(CATEGORY_PREFIX):
                handle_category(conn, action)
            elif action.data.startswith(ACCOUNT_PREFIX):
                handle_account_choice(conn, action)
            elif action.data.startswith(REMOVE_PREFIX):
                handle_remove_choice(conn, action)
            elif action.data.startswith(REFUND_PREFIX):
                handle_refund_choice(conn, action)
        except Exception:
            log_event("update.handled", status="error", update_id=update_id,
                      source="webhook", duration_ms=ms_since(start))
            raise
    log_event("update.handled", status="ok", update_id=update_id,
              source="webhook", duration_ms=ms_since(start))
    return {"ok": True}
