"""The Mini App's recurring-rule routes: pause/resume and delete (§13, §16,
§18, task 1161 split 2/2a).

Its own module, mirroring `refund.py` — `routes.py` is already at CLAUDE.md's
300-line guideline. `authenticated_user`/`permitted_user` stay put in
`routes.py`; importing them here is cheaper than a third copy. Creation has no
surface yet (undecided — a bot command or a dashboard form, task 1161 split
2/2b); these two routes only manage rules `db.recurring.create_recurring_rule`
already wrote directly, the same split `refund.py` landed in ahead of a caller.
"""

import time
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response

from kanakko.db import connect, delete_recurring_rule, set_recurring_rule_active
from kanakko.eventlog import log_event, ms_since
from kanakko.webapp.routes import authenticated_user, permitted_user

router = APIRouter()


@router.post("/app/recurring/active")
async def mini_app_recurring_active(request: Request) -> Response:
    """Pause or resume one of the household's recurring rules (§13, §16, §18).

    The dashboard's per-rule toggle POSTs `{"id": <rule_id>, "active": <bool>}`
    — the *state to move to*, read off the toggle's own `data-active` (§13's
    `recurring_list` docstring), not a flip computed here. Calls the same
    `set_recurring_rule_active` the module already exposes — household-scoped,
    not creator-scoped (§16): any member may pause a rule another member set
    up, the same reach `/app/refund` has. Mutates state, so it passes a 24h
    `max_age` (§13).

    A body without a usable integer `id` or a boolean `active` is 400; an id
    that does not resolve within the caller's household is 404. Success is 204.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        rule_id = int(body["id"])
        active = body["active"]
        if not isinstance(active, bool):
            raise TypeError(f"active must be a bool: {active!r}")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    with connect() as conn:
        user_id = permitted_user(conn, telegram_user_id)
        updated = set_recurring_rule_active(conn, user_id, rule_id, active)
    # ponytail: synchronous log write in an async route — see routes.py's
    # /app/delete for the ceiling and upgrade path. No `update_id` (§17 gap 2).
    if updated is None:
        log_event("recurring_rule.active_set", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start))
        raise HTTPException(status_code=404)
    log_event("recurring_rule.active_set", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start),
              rule_id=updated["rule_id"], active=updated["active"])
    return Response(status_code=204)


@router.post("/app/recurring/delete")
async def mini_app_recurring_delete(request: Request) -> Response:
    """Delete one of the household's recurring rules outright (§13, §16, §18).

    The dashboard's per-rule delete button POSTs `{"id": <rule_id>}`. Calls
    `delete_recurring_rule` — a hard delete, since a rule is a standing
    instruction rather than a ledger row and nothing yet references a
    `rule_id` (migration 015's comment); nothing is left to soft-delete or
    orphan. Household-scoped like the toggle above. Mutates state, so it
    passes a 24h `max_age` (§13).

    A body without a usable integer `id` is 400; an id that does not resolve
    within the caller's household is 404. Success is 204.
    """
    telegram_user_id = authenticated_user(request, max_age=timedelta(hours=24))

    try:
        body = await request.json()
        rule_id = int(body["id"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400)

    start = time.perf_counter()
    with connect() as conn:
        user_id = permitted_user(conn, telegram_user_id)
        deleted = delete_recurring_rule(conn, user_id, rule_id)
    # ponytail: synchronous log write in an async route — see routes.py's
    # /app/delete for the ceiling and upgrade path. No `update_id` (§17 gap 2).
    if deleted is None:
        log_event("recurring_rule.deleted", status="noop", source="miniapp",
                  user_id=user_id, duration_ms=ms_since(start))
        raise HTTPException(status_code=404)
    log_event("recurring_rule.deleted", status="ok", source="miniapp",
              user_id=user_id, duration_ms=ms_since(start),
              rule_id=deleted["rule_id"])
    return Response(status_code=204)
