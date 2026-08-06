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
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl

from kanakko.jobs.evening import IST
from kanakko.money import format_amount


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
    # if the dashboard ever mutates state on GET (tasks 100/101 do — see REVIEWS.md).


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


def current_month_ist(now: datetime | None = None) -> tuple[date, date]:
    """`(first_of_this_month, first_of_next_month)` in `Asia/Kolkata` (§10).

    The half-open range the dashboard's current-month figures bucket on — the
    mirror of `jobs.monthly.previous_month_ist`, shifted forward one month.
    Computing it in IST is the point: a UTC-date computation would slide the
    boundary 5.5 hours and, just after IST midnight on the 1st, put this month's
    opening entries in last month's figures. `now` defaults to the current instant.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    this_first = now.date().replace(day=1)
    # Day 28 + 4 days always lands in the next month, whatever the length.
    next_first = (this_first + timedelta(days=32)).replace(day=1)
    return this_first, next_first


def _stat(label: str, amount: Decimal) -> str:
    """One label/value pair. `amount` goes through `format_amount`, never float (§9)."""
    return (
        f'<div class="stat"><span class="label">{label}</span>'
        f'<span class="value">{format_amount(amount)}</span></div>'
    )


def dashboard_html(
    income: Decimal,
    expenses: Decimal,
    month_label: str,
    month_income: Decimal,
    month_expenses: Decimal,
) -> str:
    """The dashboard fragment: all-time totals + balance, then this month (§13).

    Balance is `income - expenses` — exact `Decimal` subtraction, can be negative.
    Server-rendered so every later section (category bars, recent list) stays
    Python + CSS with no charting library (§13). Only formatted amounts and a
    strftime month label are interpolated — no user-controlled string reaches the
    markup here, so there is nothing to escape yet.
    """
    return (
        "<h1>Kanakko</h1>"
        '<section class="totals"><h2>All time</h2>'
        + _stat("Balance", income - expenses)
        + _stat("Income", income)
        + _stat("Expenses", expenses)
        + "</section>"
        f'<section class="month"><h2>{month_label}</h2>'
        + _stat("Balance", month_income - month_expenses)
        + _stat("Income", month_income)
        + _stat("Expenses", month_expenses)
        + "</section>"
    )


# The Mini App bootstrap: Telegram opens this plain URL with no initData in it, so
# the page reads `initData` client-side and hands it to `/app/data` in the `tma`
# Authorization header, which validates it and returns the server-rendered
# figures. Contains no data and no secret, so it needs no auth itself.
SHELL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kanakko</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
<div id="app">Loading…</div>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
fetch('/app/data', {headers: {Authorization: 'tma ' + tg.initData}})
  .then(r => { if (!r.ok) throw new Error(r.status); return r.text(); })
  .then(html => { document.getElementById('app').innerHTML = html; })
  .catch(() => { document.getElementById('app').textContent = 'Could not load dashboard.'; });
</script>
</body>
</html>
"""
