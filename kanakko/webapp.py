"""Telegram Mini App `initData` validation (§13).

The dashboard's only authentication: a valid `initData` HMAC proves the payload
came from Telegram and carries the real user id, so no login form, sessions, or
OAuth ever enter the roadmap.

Verified mechanism (Telegram Mini Apps docs, §13): the fields are sorted into a
newline-joined `key=value` data-check string, and the signature is
`HMAC-SHA256(data_check_string, secret_key)` where
`secret_key = HMAC-SHA256(bot_token, "WebAppData")` — i.e. the constant
`"WebAppData"` is the key over the bot token, then that digest is the key over
the data-check string. Compared with `hmac.compare_digest`, never `==`.

Fails closed on an unset `TELEGRAM_BOT_TOKEN` (a `RuntimeError` before any
comparison), same as `tg.py` — the secret comes from the environment, never a
literal.
"""

import hashlib
import hmac
import os
from urllib.parse import parse_qsl


class InitDataError(Exception):
    """`initData` was missing, malformed, or its HMAC did not verify."""


def validate_init_data(init_data: str) -> dict[str, str]:
    """Verify a Mini App `initData` query string and return its fields.

    `init_data` is the raw `window.Telegram.WebApp.initData` string (a URL query
    string). Returns the decoded fields (including the still-JSON-encoded `user`)
    on success. Raises `InitDataError` if the `hash` is absent or does not match,
    and `RuntimeError` if `TELEGRAM_BOT_TOKEN` is unset.
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
    return fields

    # ponytail: no auth_date freshness check — §13 requires only the HMAC, and
    # replay of a stolen initData needs a stolen device. Add a max_age guard here
    # if the dashboard ever mutates state on GET.
