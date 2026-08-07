"""Telegram Mini App `initData` validation (§13) — the dashboard's only auth.

A valid `initData` HMAC proves the payload came from Telegram and carries the
real user id, so no login form, sessions, or OAuth ever enter the roadmap.

Verified mechanism (Telegram Mini Apps docs, §13): the fields are sorted into a
newline-joined `key=value` data-check string, and the signature is
`HMAC-SHA256(data_check_string, secret_key)` where
`secret_key = HMAC-SHA256(bot_token, "WebAppData")` — i.e. the constant
`"WebAppData"` is the key over the bot token, then that digest is the key over
the data-check string. Compared with `hmac.compare_digest`, never `==`.

Fails closed on an unset `TELEGRAM_BOT_TOKEN` (a `RuntimeError` before any
comparison), same as `tg.py` — the secret comes from the environment, never a
literal.

This file is security code and holds nothing else. It used to sit in the same
module as the dashboard's CSS.
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """`initData` was missing, malformed, or its HMAC did not verify."""


def validate_init_data(
    init_data: str,
    max_age: timedelta | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Verify a Mini App `initData` query string and return its fields.

    `init_data` is the raw `window.Telegram.WebApp.initData` string (a URL query
    string). Returns the decoded fields (including the still-JSON-encoded `user`)
    on success. Raises `InitDataError` if the `hash` is absent or does not match,
    and `RuntimeError` if `TELEGRAM_BOT_TOKEN` is unset.

    `max_age` opts into an `auth_date` freshness check (Telegram's documented
    replay defence; the official SDK defaults to 24h). The read-only dashboard
    leaves it `None` — a stale-but-valid HMAC only reveals the user's own data —
    but a *state-mutating* route (per-row delete, category change) must pass one,
    or a captured `initData` is a delete button that works forever. When set, a
    missing or malformed `auth_date`, or one older than `max_age`, raises
    `InitDataError`; the check runs only after the HMAC verifies, so `auth_date`
    is trusted. `now` defaults to the current instant (injectable for tests).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # keep_blank_values so an empty field still round-trips into the check string.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("initData has no hash")

    data_check_string = "\n".join(
        f"{key}={fields[key]}" for key in sorted(fields)
    )
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("initData hash mismatch")

    if max_age is not None:
        now = now or datetime.now(timezone.utc)
        try:
            auth_date = datetime.fromtimestamp(int(fields["auth_date"]), timezone.utc)
        except (KeyError, ValueError, OverflowError, OSError):
            raise InitDataError("initData auth_date is missing or malformed") from None
        if now - auth_date > max_age:
            raise InitDataError("initData is stale")
    return fields


def user_id_from_init_data(fields: dict[str, str]) -> int:
    """The Telegram user id from validated `initData` fields (§1, §13).

    `fields["user"]` is the still-JSON-encoded user object Telegram signed; its
    `id` is the authenticated Telegram user id, which a private chat also uses as
    the chat id. Raises `InitDataError` if the field is absent or not a JSON
    object with an integer `id` — a payload that verified but can't name a user is
    as unusable as one that didn't verify.
    """
    raw = fields.get("user")
    if not raw:
        raise InitDataError("initData has no user")
    try:
        uid = json.loads(raw)["id"]
    except (ValueError, TypeError, KeyError):
        raise InitDataError("initData user is malformed") from None
    if not isinstance(uid, int) or isinstance(uid, bool):
        raise InitDataError("initData user id is not an integer")
    return uid
