"""The Mini App's data routes: dashboard fragment, delete, category, edit (§13).

Split out of `kanakko/app.py`, past 300 lines once the edit route (task 974)
landed. `connect` moved here with the routes, so `tests/test_webapp.py`'s
`monkeypatch.setattr(app_module, "connect", ...)` keeps working once `app_module` names this module.
"""

import time
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from kanakko.categories import ALL_CATEGORIES
from kanakko.db import (
    EDITABLE_TRANSACTION_FIELDS,
    connect,
    edit_transaction_field,
    find_user,
    household_accounts,
    month_summary,
    recent_transactions,
    set_transaction_category,
    soft_delete_transaction,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.money import parse_amount
from kanakko.webapp import (
    InitDataError,
    Period,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    previous_month_first,
    user_id_from_init_data,
    validate_init_data,
)

router = APIRouter()

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


def permitted_user(conn, telegram_user_id: int) -> int:
    """This Mini App caller's internal `user_id`, or a 403 (§16).

    `authenticated_user` proves *which* Telegram user is asking; it never asks
    whether they are permitted. That is a separate question and this is where it
    is answered — once, rather than once per route.

    **Resolving must not create.** These routes previously called
    `get_or_create_user`, so anyone who found the bot and tapped the menu button
    minted a `users` row. That row then satisfied the bot's own gate, which asks
    `user_exists` — so the Mini App was a way around the invite gate §16 exists to
    be. Worse, the row belonged to no household, and `confirm_pending` derives
    `household_id` from a membership that wasn't there: a NOT NULL violation, a
    500, and a Telegram redelivery loop on every Confirm.

    A user row is therefore the credential: it exists only after `/start` admitted
    the person (an invite in `invite` mode, a household of one in `open` mode), so
    "no row" means "has not been admitted" in either mode and gets a 403.
    """
    user_id = find_user(conn, telegram_user_id)
    if user_id is None:
        raise HTTPException(status_code=403)
    return user_id


@router.get("/app/data", response_class=HTMLResponse)
def mini_app_data(request: Request) -> str:
    """The dashboard fragment for the authenticated user (§13): totals, balance,
    this-week and current-month figures.

    Read-only, so `authenticated_user` is called without a `max_age`: a
    stale-but-valid payload reveals only the caller's own figures.
    """
    telegram_user_id = authenticated_user(request)

    with connect() as conn:
        user_id = permitted_user(conn, telegram_user_id)
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
        accounts = household_accounts(conn, user_id)
    return dashboard_html(
        [
            Period("week", "Week", "this week", w_income, w_expenses, w_top,
                   pw_expenses, "vs last week"),
            Period("month", "Month", first.strftime("%B %Y"), m_income, m_expenses,
                   m_top, pm_expenses, "vs last month"),
            Period("all", "All", "all time", a_income, a_expenses, a_top),
        ],
        recent,
        accounts=accounts,
    )


@router.post("/app/delete")
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
        user_id = permitted_user(conn, telegram_user_id)
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


@router.post("/app/category")
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
        user_id = permitted_user(conn, telegram_user_id)
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


def _parsed_edit_value(field: str, raw):
    """`raw` (always a string off the wire) parsed to the type `field`'s column
    needs (§9, task 974). Raises `ValueError`/`TypeError` on anything unusable,
    which `mini_app_edit` turns into a 400 — the same contract `parse_amount`
    already has for the bot's own amount parsing.
    """
    if not isinstance(raw, str):
        raise TypeError(f"edit value must be a string: {raw!r}")
    if field == "amount":
        return parse_amount(raw)
    if field == "occurred_on":
        return date.fromisoformat(raw)
    if field == "note":
        text = raw.strip()
        return text or None
    return int(raw)  # account_id


@router.post("/app/edit")
async def mini_app_edit(request: Request) -> Response:
    """Change one field — amount, date, note, or account — of one of the user's
    transactions (§5, §13, §16, §18, task 974).

    §5 rejected a field editor in the bot chat but named this Mini App list as
    the cheaper replacement for anything older than the last entry; category
    already has its own control (`/app/category`), this covers the rest. The
    dashboard's per-row edit panel POSTs `{"id": <txn_id>, "field": <name>,
    "value": <string>}`; `field` must be one of `EDITABLE_TRANSACTION_FIELDS`, so
    a forged body cannot target an arbitrary column. Like `/app/category` this
    *mutates state*, so it passes a 24h `max_age` (§13). The row is scoped to the
    authenticated user, so one user cannot edit another's — §16: "Edit / delete a
    row: only the member who entered it".

    A body without a usable `id`/`field`, an unknown `field`, or a `value` that
    fails to parse for that field (a non-numeric amount, an unparsable date) is
    400; an id matching no live row of this user's — or an `account_id` edit that
    does not apply (a transfer, or an id outside this household's live,
    non-`external` accounts) — is 404. Success is 204.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        txn_id = int(body["id"])
        field = body["field"]
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)
    if field not in EDITABLE_TRANSACTION_FIELDS:
        raise HTTPException(status_code=400)
    try:
        value = _parsed_edit_value(field, body["value"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    with connect() as conn:
        user_id = permitted_user(conn, telegram_user_id)
        updated = edit_transaction_field(conn, user_id, txn_id, field, value,
                                         source="miniapp", update_id=None)
    # ponytail: synchronous log write in an async route — see /app/delete above
    # for the ceiling and upgrade path. No `update_id` (§17 gap 2); 404 is `noop`.
    if updated is None:
        log_event("transaction.edited", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start), field=field)
        raise HTTPException(status_code=404)
    log_event("transaction.edited", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start), field=field,
              txn_id=updated["txn_id"], amount=updated["amount"])
    return Response(status_code=204)
