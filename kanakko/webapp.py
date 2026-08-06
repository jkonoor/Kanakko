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
import html
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl

from kanakko.categories import CATEGORIES_BY_TYPE
from kanakko.jobs.evening import IST
from kanakko.money import format_amount


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


def current_week_ist(now: datetime | None = None) -> tuple[date, date]:
    """`(monday_of_this_week, next_monday)` in `Asia/Kolkata` (§10, §13).

    The half-open range the dashboard's this-week figures bucket on. Weeks start
    Monday (`date.weekday()`: Mon=0). Computed in IST for the same reason as
    `current_month_ist`: just after IST midnight a UTC-date computation would
    slide the boundary 5.5 hours and, on a Monday, drop the week's opening entries
    into last week. `now` defaults to the current instant.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    monday = now.date() - timedelta(days=now.date().weekday())
    return monday, monday + timedelta(days=7)


def _stat(label: str, amount: Decimal) -> str:
    """One label/value pair. `amount` goes through `format_amount`, never float (§9)."""
    return (
        f'<div class="stat"><span class="label">{label}</span>'
        f'<span class="value">{format_amount(amount)}</span></div>'
    )


def category_bars(categories: list[tuple[str, Decimal]], total: Decimal) -> str:
    """This month's expense categories as a sorted list with CSS percentage bars (§13).

    `categories` is `(name, amount)` biggest-first (straight from
    `db.month_summary`'s `top`); `total` is the month's expenses, the denominator
    each bar's width is a share of. No charting library — the bar is a `<div>`
    whose inline `width` is the category's percentage of `total`, exact `Decimal`
    arithmetic so no float touches an amount (§9). Category names are HTML-escaped:
    they come from the fixed `categories.py` set today, but escaping keeps the
    fragment safe if a user-named category ever reaches it. An empty month, or a
    `total` of 0, yields no section.
    """
    if not categories or total <= 0:
        return ""
    rows = []
    for name, amount in categories:
        pct = amount / total * 100  # Decimal / Decimal — never float
        rows.append(
            '<div class="cat">'
            f'<div class="cat-head"><span>{html.escape(name)}</span>'
            f'<span>{format_amount(amount)}</span></div>'
            f'<div class="bar"><div class="fill" style="width:{pct:.1f}%"></div></div>'
            "</div>"
        )
    return (
        '<section class="breakdown"><h2>Spending by category</h2>'
        + "".join(rows)
        + "</section>"
    )


def _category_select(txn_id: int, type_: str, current: str | None) -> str:
    """A per-row category `<select>` for the recent list (§5, §13, task 101).

    Offers the closed set for this transaction's `type_` (`categories.py`), the
    current category pre-selected. A null/unknown category shows a disabled
    "Uncategorised" placeholder so the picker still names one in one change. The
    option *value* the browser reports is the decoded name (`html.escape` only
    affects rendering), so `POST /app/category` receives the literal category. The
    `data-id` carries the `txn_id`. Category names come from the fixed set today,
    but escaping keeps the markup safe if a user-named category ever reaches it.
    """
    known = current in CATEGORIES_BY_TYPE.get(type_, ())
    opts = []
    if not known:
        opts.append('<option value="" disabled selected>Uncategorised</option>')
    for c in CATEGORIES_BY_TYPE.get(type_, ()):
        sel = " selected" if c == current else ""
        opts.append(f"<option{sel}>{html.escape(c)}</option>")
    return (
        f'<select class="cat-select" data-id="{txn_id}">'
        + "".join(opts)
        + "</select>"
    )


def recent_list(rows: list[tuple]) -> str:
    """The recent-transactions list with per-row delete + category change (§13, tasks 100–101).

    Each `rows` entry is `(txn_id, amount, type, category, note, occurred_on)`
    from `db.recent_transactions`. `note` is the first *user-typed* string the
    dashboard renders — §11 keeps the note's original wording, so it is arbitrary
    text that arrived through the bot — and it is HTML-escaped: an unescaped
    `<img src=x onerror=...>` in a logged expense would be stored XSS. The category
    is a per-row `<select>` (a null category is "Uncategorised") that POSTs to
    `/app/category`. Amounts go through `format_amount` (§9), prefixed −/+ by
    direction. The delete button carries the `txn_id` for `POST /app/delete`. An
    empty ledger renders nothing.
    """
    if not rows:
        return ""
    items = []
    for txn_id, amount, type_, category, note, occurred_on in rows:
        sign = "−" if type_ == "expense" else "+"
        note_html = (
            f'<div class="txn-note">{html.escape(note)}</div>' if note else ""
        )
        items.append(
            '<div class="txn">'
            f'<div class="txn-main"><span>{occurred_on:%d %b} · '
            + _category_select(txn_id, type_, category)
            + f'</span><span>{sign}{format_amount(amount)}</span></div>'
            + note_html
            + f'<button class="del" data-id="{txn_id}">✕</button>'
            "</div>"
        )
    return '<section class="recent"><h2>Recent</h2>' + "".join(items) + "</section>"


def dashboard_html(
    income: Decimal,
    expenses: Decimal,
    week_income: Decimal,
    week_expenses: Decimal,
    month_label: str,
    month_income: Decimal,
    month_expenses: Decimal,
    top: list[tuple[str, Decimal]],
    recent: list[tuple],
) -> str:
    """The dashboard fragment: totals + balance, this week/month, bars, recent list (§13).

    Balance is `income - expenses` — exact `Decimal` subtraction, can be negative.
    Server-rendered so every section (week/month figures, category bars, recent
    list) stays Python + CSS with no charting library (§13). The week and month
    summaries carry each period's income, expenses, and balance; `top` is the
    month's expense categories biggest-first, rendered as
    percentage-of-month-expenses bars; `recent` is the recent-transactions list
    with per-row delete. Formatted amounts, a strftime month label, and escaped
    category names / notes are interpolated — the note is the only user-typed
    string and `recent_list` escapes it, so nothing reaches the markup unescaped.
    """
    return (
        "<h1>Kanakko</h1>"
        '<section class="totals"><h2>All time</h2>'
        + _stat("Balance", income - expenses)
        + _stat("Income", income)
        + _stat("Expenses", expenses)
        + "</section>"
        '<section class="week"><h2>This week</h2>'
        + _stat("Balance", week_income - week_expenses)
        + _stat("Income", week_income)
        + _stat("Expenses", week_expenses)
        + "</section>"
        f'<section class="month"><h2>{month_label}</h2>'
        + _stat("Balance", month_income - month_expenses)
        + _stat("Income", month_income)
        + _stat("Expenses", month_expenses)
        + "</section>"
        + category_bars(top, month_expenses)
        + recent_list(recent)
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
<style>
/* Follow Telegram's theme: it injects --tg-theme-* CSS vars, so binding the
   background and text to them makes the page dark in a dark theme and light in
   a light one. Without this the body keeps the browser default (black on white)
   and shows as a glaring white panel inside Telegram's dark chrome. Fallbacks
   are the light-theme values for a plain-browser open. */
body {
  font-family: system-ui, sans-serif; margin: 0; padding: 16px;
  background: var(--tg-theme-bg-color, #fff);
  color: var(--tg-theme-text-color, #000);
}
.label, .txn-note { color: var(--tg-theme-hint-color, #707579); }
.stat, .cat-head { display: flex; justify-content: space-between; }
.cat { margin: 8px 0; }
.bar { background: rgba(128,128,128,.2); border-radius: 4px; height: 8px; overflow: hidden; }
.fill { background: var(--tg-theme-button-color, #3390ec); height: 100%; }
/* Grid, not wrapping flex: the note needs its own row while the delete button
   stays on the first one. As sibling flex items with `.txn-note` at
   flex-basis:100%, `.del` was pushed onto a third line and the rows collided —
   visible only in a browser, which is why the test can guard just the structure
   that makes this work (see test_txn_row_places_note_and_delete). */
.txn { display: grid; grid-template-columns: 1fr auto; column-gap: 8px; align-items: center; margin: 8px 0; }
.txn-main { display: flex; justify-content: space-between; gap: 8px; min-width: 0; }
.txn-note { grid-column: 1; font-size: .9em; }
.del { grid-row: 1; grid-column: 2; border: none; background: none; cursor: pointer; color: inherit; }
.cat-select { background: none; border: none; color: inherit; font: inherit; cursor: pointer; }
</style>
</head>
<body>
<div id="app">Loading…</div>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
const app = document.getElementById('app');
function load() {
  fetch('/app/data', {headers: {Authorization: 'tma ' + tg.initData}})
    .then(r => { if (!r.ok) throw new Error(r.status); return r.text(); })
    .then(html => { app.innerHTML = html; })
    .catch(() => { app.textContent = 'Could not load dashboard.'; });
}
app.addEventListener('click', e => {
  const btn = e.target.closest('.del');
  if (!btn) return;
  fetch('/app/delete', {
    method: 'POST',
    headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
    body: JSON.stringify({id: Number(btn.dataset.id)}),
  }).then(r => { if (r.ok) load(); });
});
app.addEventListener('change', e => {
  const sel = e.target.closest('.cat-select');
  if (!sel || !sel.value) return;
  fetch('/app/category', {
    method: 'POST',
    headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
    body: JSON.stringify({id: Number(sel.dataset.id), category: sel.value}),
  }).then(r => { if (r.ok) load(); });
});
load();
</script>
</body>
</html>
"""
