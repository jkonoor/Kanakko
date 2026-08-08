"""FastAPI app: webhook and Mini App routes."""

import hmac
import os
import time
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from kanakko import __version__, configure_logging
from kanakko.auth import is_authorized, within_daily_cap
from kanakko.categories import ALL_CATEGORIES, CATEGORY_PREFIX
from kanakko.confirm import CANCEL, CONFIRM
from kanakko.db import (
    claim_update,
    connect,
    get_or_create_user,
    month_summary,
    recent_transactions,
    set_transaction_category,
    soft_delete_transaction,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import (
    ACCESS_REFUSED,
    CAP_REACHED,
    TextMessage,
    _is_household,
    _is_invite,
    _is_remove,
    _is_start,
    _is_undo,
    dispatch,
    handle_cancel,
    handle_category,
    handle_confirm,
    handle_household,
    handle_invite,
    handle_remove,
    handle_start,
    handle_text,
    handle_undo,
)
from kanakko.tg import send_message
from kanakko.webapp import (
    SHELL_HTML,
    InitDataError,
    Period,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    previous_month_first,
    user_id_from_init_data,
    validate_init_data,
)

configure_logging()

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

def authenticated_user(request: Request, max_age: timedelta | None = None) -> int:
    """The Telegram user id proved by this request's `initData`, or a 401 (§13).

    Every Mini App route starts here, so the check exists once rather than once
    per route — the third copy of an auth preamble is where they start to drift.
    The bootstrap sends `initData` in `Authorization: tma <initData>` (Telegram's
    documented scheme); validation *is* the authentication, so a valid HMAC both
    proves the payload came from Telegram and names the user (§13).

    A missing, malformed, forged or (with `max_age`) stale payload is a 401. An
    unset `TELEGRAM_BOT_TOKEN` is left to raise, surfacing as a 500 — a loud
    misconfiguration rather than a quiet bypass.

    `max_age` is the replay window, and **state-mutating routes must pass one**
    (§13): a captured `initData` must not stay a working delete button forever.
    Read-only routes leave it `None`, where a stale-but-valid payload only ever
    reveals the caller's own data.
    """
    header = request.headers.get("Authorization") or ""
    if not header.startswith(TMA_PREFIX):
        raise HTTPException(status_code=401)
    try:
        fields = validate_init_data(header[len(TMA_PREFIX):], max_age=max_age)
        return user_id_from_init_data(fields)
    except InitDataError:
        raise HTTPException(status_code=401)



@app.get("/app/data", response_class=HTMLResponse)
def mini_app_data(request: Request) -> str:
    """The dashboard fragment for the authenticated user (§13): totals, balance,
    this-week and current-month figures.

    Read-only, so `authenticated_user` is called without a `max_age`: a
    stale-but-valid payload reveals only the caller's own figures.
    """
    telegram_user_id = authenticated_user(request)

    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        first, next_first = current_month_ist()
        w_first, w_next = current_week_ist()
        # month_summary is a generic date-range summary, so the same function
        # serves all three periods — the week, the month, and (over a range wide
        # enough to hold any ledger) all time. Each panel needs its *own* category
        # breakdown, or switching to Week would show the month's bars underneath
        # the week's figures.
        w_income, w_expenses, w_top = month_summary(conn, user_id, w_first, w_next)
        m_income, m_expenses, m_top = month_summary(conn, user_id, first, next_first)
        a_income, a_expenses, a_top = month_summary(
            conn, user_id, date.min, date.max
        )
        # Previous-period expenses give each hero a baseline — a figure with none
        # is a record, not an insight. Same generic query, shifted bounds: the week
        # before (a plain 7-day step back) and the month before (its 1st, which for
        # January is December of the prior year — see `previous_month_first`). Both
        # derive from the *current* bounds, so no second clock read can disagree
        # with them at a month/week rollover. All-time has no prior period.
        prev_m_first = previous_month_first(first)
        _, pw_expenses, _ = month_summary(
            conn, user_id, w_first - timedelta(days=7), w_first
        )
        _, pm_expenses, _ = month_summary(conn, user_id, prev_m_first, first)
        recent = recent_transactions(conn, user_id)
    return dashboard_html(
        [
            Period("week", "Week", "this week", w_income, w_expenses, w_top,
                   pw_expenses, "vs last week"),
            Period("month", "Month", first.strftime("%B %Y"), m_income, m_expenses,
                   m_top, pm_expenses, "vs last month"),
            Period("all", "All", "all time", a_income, a_expenses, a_top),
        ],
        recent,
    )


@app.post("/app/delete")
async def mini_app_delete(request: Request) -> Response:
    """Soft-delete one of the authenticated user's transactions (§13, task 100).

    The dashboard's per-row delete button POSTs `{"id": <txn_id>}`. This route
    *mutates state*, so it passes a 24h `max_age` — a captured `initData` must
    not stay a working delete button forever (§13, and the official SDK's
    default). The row is scoped to the user `authenticated_user` returns, resolved
    from the *signed* `user` object, so one user cannot delete another's row by
    guessing an id.

    A body without a usable integer `id` is 400; a delete that matched no live row
    of this user's is 404 (already gone, or never theirs). Success is 204 — the
    client re-fetches `/app/data`.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        txn_id = int(body["id"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        deleted = soft_delete_transaction(conn, user_id, txn_id,
                                          source="miniapp", update_id=None)
    # ponytail: log_event is a synchronous write inside an async route — fine at
    # this scale (one line, no fsync). Move to a queue only if the sink ever
    # blocks the event loop. No `update_id`: a Mini App POST is not a Telegram
    # update (§17 gap 2). A 404 (already gone, or never this user's) is `noop`.
    if deleted is None:
        log_event("transaction.deleted", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start))
        raise HTTPException(status_code=404)
    log_event("transaction.deleted", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start),
              txn_id=deleted["txn_id"], amount=deleted["amount"])
    return Response(status_code=204)


@app.post("/app/category")
async def mini_app_category(request: Request) -> Response:
    """Change the category of one of the user's transactions (§5, §13, task 101).

    The dashboard's per-row category `<select>` POSTs
    `{"id": <txn_id>, "category": <name>}`. Like `/app/delete` this *mutates
    state*, so it passes a 24h `max_age` (§13). `category` must be one of the
    closed set (`categories.py`, §11); anything else is a 400, so a forged body
    cannot write a junk label. The row is scoped to the authenticated user, so one
    user cannot relabel another's.

    A body without a usable integer `id`, or an unknown `category`, is 400; an id
    matching no live row of this user's is 404. Success is 204.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        txn_id = int(body["id"])
        category = body["category"]
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)
    if category not in ALL_CATEGORIES:
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    with connect() as conn:
        user_id = get_or_create_user(conn, telegram_user_id)
        updated = set_transaction_category(conn, user_id, txn_id, category,
                                           source="miniapp", update_id=None)
    # ponytail: synchronous log write in an async route — see /app/delete above
    # for the ceiling and upgrade path. No `update_id` (§17 gap 2); 404 is `noop`.
    if updated is None:
        log_event("transaction.recategorised", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start))
        raise HTTPException(status_code=404)
    log_event("transaction.recategorised", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start),
              txn_id=updated["txn_id"], amount=updated["amount"])
    return Response(status_code=204)


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
        # §16 cost control: only the text-parse path is an LLM call (§2), so a
        # runaway user is an unbounded bill on the owner's OpenRouter credits.
        # /undo and the Confirm/Cancel/category taps cost nothing — they are never
        # capped (a user at the cap can still finish or undo a pending card) and,
        # crucially, never *metered*: the claim below stamps `user_id` only on the
        # parse path, leaving callback/undo rows NULL so `count_updates_on_day`
        # counts exactly the messages that spend credits (§16), not the free taps.
        is_parse = isinstance(action, TextMessage) and not (
            _is_undo(action.text)
            or _is_invite(action.text)
            or _is_household(action.text)
            or _is_remove(action.text)
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
                if _is_undo(action.text):
                    handle_undo(conn, action)
                elif _is_invite(action.text):
                    handle_invite(conn, action)
                elif _is_household(action.text):
                    handle_household(conn, action)
                elif _is_remove(action.text):
                    handle_remove(conn, action)
                else:
                    handle_text(conn, action)
            elif action.data == CONFIRM:
                handle_confirm(conn, action)
            elif action.data == CANCEL:
                handle_cancel(conn, action)
            elif action.data.startswith(CATEGORY_PREFIX):
                handle_category(conn, action)
        except Exception:
            log_event("update.handled", status="error", update_id=update_id,
                      source="webhook", duration_ms=ms_since(start))
            raise
    log_event("update.handled", status="ok", update_id=update_id,
              source="webhook", duration_ms=ms_since(start))
    return {"ok": True}
