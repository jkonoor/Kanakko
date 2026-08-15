"""The Mini App's refund route: `POST /app/refund` (§13, §16, §18, task 1082).

Its own module rather than a fifth route in `kanakko.webapp.routes` — that file
already sits at CLAUDE.md's 300-line guideline (task 974's docstring says so),
and this route's body is close to `/app/edit`'s in size, so a sixth would push
it over. `authenticated_user`/`permitted_user` stay put in `routes.py`;
importing them here is cheaper than a third copy or a bigger reshuffle of the
existing, working routes.
"""

import time
from datetime import date, timedelta

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response

from kanakko.db import connect, create_refund
from kanakko.eventlog import log_event, ms_since
from kanakko.money import parse_amount
from kanakko.parse import today
from kanakko.webapp.routes import authenticated_user, permitted_user

router = APIRouter()


@router.post("/app/refund")
async def mini_app_refund(request: Request) -> Response:
    """Refund part or all of one of the household's expenses (§13, §16, §18).

    The dashboard's per-row refund panel (`kanakko.webapp.recent._refund_panel`)
    POSTs `{"id": <the expense's txn_id>, "amount": <string>}`. Calls the same
    `create_refund` the bot's `handle_refund_choice` does — same trigger-enforced
    "sum of refunds against a transaction never exceeds it" guard (§18, migration
    014) — rather than a second write path for the Mini App. Household-scoped,
    not user-scoped, matching `create_refund` itself (§16): any member may refund
    any member's expense, unlike `/app/edit`'s per-row restriction to whoever
    entered it. Like `/app/edit` this *mutates state*, so it passes a 24h
    `max_age` (§13).

    A body without a usable `id`/`amount` is 400. An over-limit refund — a second
    member refunding the same expense between the panel opening and this tap, or
    a stale value left in the input — is caught by the trigger's
    `RaiseException` and answered 409, distinct from the 404 a missing, foreign,
    or non-expense row gets, so the client can tell "nothing there" from "too
    much". Success is 204 — the client re-fetches `/app/data`.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        txn_id = int(body["id"])
        amount = parse_amount(body["amount"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    try:
        with connect() as conn:
            user_id = permitted_user(conn, telegram_user_id)
            refunded = create_refund(conn, user_id, txn_id, amount,
                                     date.fromisoformat(today()),
                                     source="miniapp", update_id=None)
    except psycopg.errors.RaiseException:
        log_event("refund.created", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start),
                  outcome="over_limit")
        raise HTTPException(status_code=409)

    # ponytail: synchronous log write in an async route — see routes.py's
    # /app/delete for the ceiling and upgrade path. No `update_id` (§17 gap 2).
    if refunded is None:
        log_event("refund.created", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start))
        raise HTTPException(status_code=404)
    log_event("refund.created", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start),
              txn_id=refunded["txn_id"], amount=amount)
    return Response(status_code=204)
