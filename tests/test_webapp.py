"""Mini App `initData` HMAC validation (§13) — no network.

What breaks silently if this is wrong: a forged or tampered `initData` is
accepted, and anyone can open the dashboard as any user. So the guards assert
the *effect* — a good payload verifies and returns its user, a byte-flipped one
and a re-signed-with-the-wrong-token one both raise.
"""

import hashlib
import hmac
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from conftest import default_account_of, household_of, join_household
from fastapi.testclient import TestClient

from kanakko import eventlog
from kanakko.app import app
from kanakko.migrate import migrate
from kanakko.webapp import (
    SHELL_HTML,
    InitDataError,
    Period,
    account_balances_section,
    category_bars,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    previous_month_first,
    recent_list,
    recurring_list,
    user_id_from_init_data,
    validate_init_data,
)
from kanakko.webapp import recurring as recurring_module
from kanakko.webapp import refund as refund_module
from kanakko.webapp import routes as app_module

TOKEN = "123456:AA-test-token"


def _sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Build a valid `initData` string for `fields`, signed with `token`."""
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


FIELDS = {"auth_date": "1700000000", "user": '{"id":42,"first_name":"Ann"}'}


def test_valid_init_data_verifies_and_returns_fields(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    fields = validate_init_data(_sign(FIELDS))
    assert fields["auth_date"] == "1700000000"
    assert fields["user"] == '{"id":42,"first_name":"Ann"}'
    assert "hash" not in fields


def test_tampered_field_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    init_data = _sign(FIELDS)
    # Swap the signed user id for a different one, keeping the original hash.
    forged = init_data.replace("%3A42%2C", "%3A99%2C")
    assert forged != init_data
    with pytest.raises(InitDataError):
        validate_init_data(forged)


def test_signature_from_a_different_token_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    forged = _sign(FIELDS, token="999999:attacker-token")
    with pytest.raises(InitDataError):
        validate_init_data(forged)


def test_signature_field_stays_in_the_data_check_string(monkeypatch):
    """A Bot API 8.0+ payload carrying `signature` must still verify (§13).

    `signature` is Telegram's *separate* Ed25519 signature, for third parties
    validating without the bot token. Only that path excludes it: Telegram
    computes the bot-token HMAC over `signature` like any other field, so
    dropping it here would reject every real client that sends one.

    Verified against the official SDK, which has both paths in one file
    (Telegram-Mini-Apps/telegram-apps, `packages/init-data-node/src/validation.ts`):
    the Ed25519 path skips `hash` *and* `signature` (L94-100), the bot-token HMAC
    path skips only `hash` (L251-264). The docs state the exclusion for the
    Ed25519 path alone ("except _hash_ and _signature_") and say nothing about it
    for the HMAC path — which is exactly why a reviewer talked themselves into
    `fields.pop("signature")` here once. This test is what makes that fail.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    signed = {**FIELDS, "signature": "Ed25519_sig_from_telegram"}
    fields = validate_init_data(_sign(signed))
    assert fields["signature"] == "Ed25519_sig_from_telegram"


def test_stale_auth_date_is_rejected(monkeypatch):
    """A valid HMAC whose `auth_date` predates `max_age` is rejected (§13, task 100).

    The replay defence for the state-mutating routes: `FIELDS`'s `auth_date` is
    1700000000 (2023), so a `now` two days later with a 24h window is stale, even
    though the signature is genuine. Without this, a captured `initData` is a
    delete button that works forever.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    now = datetime.fromtimestamp(1700000000, timezone.utc) + timedelta(days=2)
    with pytest.raises(InitDataError):
        validate_init_data(_sign(FIELDS), max_age=timedelta(hours=24), now=now)


def test_fresh_auth_date_passes(monkeypatch):
    """The same genuine payload verifies while its `auth_date` is inside `max_age`."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    now = datetime.fromtimestamp(1700000000, timezone.utc) + timedelta(hours=1)
    fields = validate_init_data(_sign(FIELDS), max_age=timedelta(hours=24), now=now)
    assert fields["user"] == '{"id":42,"first_name":"Ann"}'


def test_missing_auth_date_fails_closed_when_max_age_set(monkeypatch):
    """With `max_age` set, a payload that carries no `auth_date` is rejected, not
    admitted — the freshness check fails closed (§13)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    no_date = {"user": '{"id":42,"first_name":"Ann"}'}
    with pytest.raises(InitDataError):
        validate_init_data(_sign(no_date), max_age=timedelta(hours=24))


def test_missing_hash_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(urlencode(FIELDS))


def test_fails_closed_without_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        validate_init_data(_sign(FIELDS))


# --- Dashboard route: totals, balance, current-month figures (§13, task 97) ---

client = TestClient(app)


def test_user_id_from_init_data_reads_the_signed_user():
    assert user_id_from_init_data({"user": '{"id":42,"first_name":"Ann"}'}) == 42


@pytest.mark.parametrize(
    "fields",
    [
        {},  # no user field at all
        {"user": ""},  # empty
        {"user": "not json"},  # unparseable
        {"user": "[1,2,3]"},  # JSON, but not an object with an id
        {"user": '{"first_name":"Ann"}'},  # object, no id
        {"user": '{"id":"42"}'},  # id present but a string, not an int
    ],
)
def test_user_id_from_init_data_rejects_a_missing_or_malformed_user(fields):
    with pytest.raises(InitDataError):
        user_id_from_init_data(fields)


def _periods(week=(Decimal("0"), Decimal("0")), month=(Decimal("0"), Decimal("0")),
             all_=(Decimal("0"), Decimal("0")), week_top=(), month_top=(), all_top=()):
    """Three periods in tab order, so tests name only the figures they care about."""
    return [
        Period("week", "Week", "this week", week[0], week[1], list(week_top)),
        Period("month", "Month", "August 2026", month[0], month[1], list(month_top)),
        Period("all", "All", "all time", all_[0], all_[1], list(all_top)),
    ]


def test_dashboard_html_shows_rupee_amounts_and_exact_balance():
    """Balance is exact `Decimal` subtraction — the money invariant (§9)."""
    html = dashboard_html(_periods(
        week=(Decimal("0.70"), Decimal("0.50")),
        all_=(Decimal("20000.00"), Decimal("500.50")),
    ), [])
    assert "₹20,000.00" in html  # all-time income
    assert "₹500.50" in html  # all-time expenses
    assert "₹19,499.50" in html  # all-time balance, to the paise
    assert "₹0.20" in html  # this week's balance: 0.70 - 0.50, no float drift
    assert "this week" in html
    assert "August 2026" in html


def test_dashboard_html_period_balances_are_distinct():
    """Each period panel renders its own balance, not a shared one (§13)."""
    html = dashboard_html(_periods(
        week=(Decimal("30"), Decimal("10")),   # balance 20
        month=(Decimal("80"), Decimal("25")),  # balance 55
        all_=(Decimal("100"), Decimal("40")),  # balance 60
    ), [])
    assert "₹20.00" in html  # week balance: 30 - 10
    assert "₹55.00" in html  # month balance: 80 - 25
    assert "₹60.00" in html  # all-time balance: 100 - 40


def test_dashboard_html_shows_one_panel_and_hides_the_others():
    """Only the selected period is visible; the rest ship hidden, one tap away (§13).

    This is what replaced three stacked Balance/Income/Expenses blocks. If the
    `hidden` attribute were dropped the page would render all three at once —
    the redundancy the switcher exists to remove — and every test asserting a
    figure "is in the html" would still pass, so the visibility is asserted here
    rather than left implied.
    """
    html = dashboard_html(_periods(), [], selected="week")
    assert '<section class="panel" data-period="week">' in html  # shown
    assert '<section class="panel" data-period="month" hidden>' in html
    assert '<section class="panel" data-period="all" hidden>' in html
    # and exactly one tab is marked selected, for assistive tech as well as CSS
    assert html.count('aria-selected="true"') == 1
    assert 'data-period="week" aria-selected="true"' in html


def test_category_bars_are_sorted_shares_of_month_expenses():
    """Each bar's width is the category's exact percentage of the month's expenses
    (§9, §13). A float denominator would drift these; `Decimal` arithmetic pins
    75.0% / 25.0% exactly. Bars appear biggest-first, as `month_summary` returns them.
    """
    top = [("Food", Decimal("300.00")), ("Transport", Decimal("100.00"))]
    bars = category_bars(top, Decimal("400.00"))
    assert "width:75.0%" in bars  # 300 / 400
    assert "width:25.0%" in bars  # 100 / 400
    assert bars.index("Food") < bars.index("Transport")  # biggest first
    assert "₹300.00" in bars and "₹100.00" in bars


def test_category_bars_empty_when_no_expenses():
    """No expense categories, or a zero total, renders nothing — no divide-by-zero."""
    assert category_bars([], Decimal("0")) == ""
    assert category_bars([("Food", Decimal("0"))], Decimal("0")) == ""


def test_dashboard_html_renders_the_category_breakdown():
    """The breakdown section reaches the fragment `dashboard_html` builds (§13)."""
    html = dashboard_html(_periods(
        month=(Decimal("1000"), Decimal("400")),
        month_top=[("Food", Decimal("400.00"))],
    ), [])
    assert "Spending by category" in html
    assert "width:100.0%" in html  # the sole category is all the spending


def test_each_period_gets_its_own_breakdown():
    """A period's bars are that period's, not the month's shown under every tab.

    The route computes a separate `top` per period for this reason: switching to
    Week while the bars still showed the month's categories would put figures and
    breakdown in silent disagreement — the sort of wrongness that looks fine.
    """
    html = dashboard_html(_periods(
        week=(Decimal("0"), Decimal("100")), week_top=[("Transport", Decimal("100.00"))],
        month=(Decimal("0"), Decimal("400")), month_top=[("Food", Decimal("400.00"))],
    ), [])
    # Anchor on the panel element, not the bare attribute: the tab buttons carry
    # the same `data-period`, and slicing from those runs across the wrong
    # section entirely. (Third time today a string-matching guard found the
    # earlier, wrong occurrence.)
    def panel(key: str) -> str:
        return html.split(f'<section class="panel" data-period="{key}"', 1)[1].split(
            "</section>", 1)[0]

    week_panel, month_panel = panel("week"), panel("month")
    assert "Transport" in week_panel and "Food" not in week_panel
    assert "Food" in month_panel and "Transport" not in month_panel


def test_previous_month_first_steps_january_back_to_december():
    """January's previous month is December of the *prior year* (§10, task Phase 7).

    The one boundary a naive `month - 1` gets wrong, and the case the delta must
    cover — the previous-period baseline for a January dashboard is December.
    """
    assert previous_month_first(date(2026, 1, 1)) == date(2025, 12, 1)
    # A mid-year month is unremarkable, but pin it so the helper can't regress the
    # common case while getting January right.
    assert previous_month_first(date(2026, 8, 1)) == date(2026, 7, 1)


def _delta_period(expenses, prev_expenses):
    """A month panel with only the figures the delta reads set."""
    return [Period("month", "Month", "August 2026", Decimal("0"), Decimal(expenses),
                   [], Decimal(prev_expenses) if prev_expenses is not None else None,
                   "vs last month")]


def test_hero_delta_says_no_comparison_against_a_zero_baseline():
    """A first-ever period has no baseline: "no comparison yet", never a fabricated
    percentage (a delta against zero is undefined, not 100%)."""
    html = dashboard_html(_delta_period("500", "0"), [])
    assert "no comparison yet" in html
    assert "%" not in html  # no fabricated percentage anywhere on the panel


def test_hero_delta_shows_direction_and_magnitude():
    """Two periods: the sign (arrow) and magnitude must be right, both directions."""
    down = dashboard_html(_delta_period("300", "500"), [])  # spent less
    assert "▼ 40% vs last month" in down  # (300-500)/500 = -40%
    up = dashboard_html(_delta_period("300", "200"), [])  # spent more
    assert "▲ 50% vs last month" in up  # (300-200)/200 = +50%


def test_all_time_panel_carries_no_delta():
    """An all-time range has no prior period, so its hero shows no delta at all."""
    period = [Period("all", "All", "all time", Decimal("0"), Decimal("500"), [])]
    html = dashboard_html(period, [], selected="all")
    assert "vs last" not in html
    assert "no comparison" not in html
    assert 'class="delta"' not in html


def test_current_month_ist_buckets_in_kolkata():
    """00:30 IST on Aug 1 (= 19:00 UTC Jul 31) is August, not July (§10)."""
    now = datetime(2026, 7, 31, 19, 0, tzinfo=timezone.utc)
    first, next_first = current_month_ist(now)
    assert first == date(2026, 8, 1)
    assert next_first == date(2026, 9, 1)


def test_current_week_ist_buckets_in_kolkata():
    """00:30 IST on Mon Aug 10 (= 19:00 UTC Sun Aug 9) is the week starting that
    Monday, not the previous one (§10).

    A UTC-date computation would still read Sunday Aug 9 and open the week on
    Mon Aug 3 — sliding Monday's opening entries into last week. The IST
    computation opens it on Aug 10.
    """
    now = datetime(2026, 8, 9, 19, 0, tzinfo=timezone.utc)  # Sun 19:00 UTC
    start, next_start = current_week_ist(now)
    assert start == date(2026, 8, 10)  # Monday, in IST
    assert next_start == date(2026, 8, 17)
    assert start.weekday() == 0
    # The UTC-date week would open a week earlier — that's the slide this guards.
    utc_date = now.date()
    assert start != utc_date - timedelta(days=utc_date.weekday())


def _insert_txn(conn, user_id, amount, type_, category, occurred_on, note=""):
    """Insert a transaction homed in the user's household (§16), returning its id."""
    hh = household_of(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, note, occurred_on, account_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING txn_id",
            (user_id, hh, Decimal(amount), type_, category, note, occurred_on, default_account_of(conn, hh)),
        )
        (txn_id,) = cur.fetchone()
    return txn_id


class _Reuse:
    """`connect()` stand-in that yields the shared test connection without closing it."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def test_dashboard_route_renders_totals_and_current_month(conn, monkeypatch):
    """`GET /app/data` with valid initData returns all-time totals *and* this
    month's figures, distinct from each other (§13).

    Drives the real route + `db.totals`/`db.month_summary` against Postgres; only
    the connection is redirected to the test cluster. An expense in a prior month
    lands in the all-time figures but not the current-month ones, so the two
    sections can't be silently the same number.
    """
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    first, _ = current_month_ist()
    # A day guaranteed in the previous month: the day before this month's 1st.
    prev_month_day = first - timedelta(days=1)
    _insert_txn(conn, uid, "20000.00", "income", "Salary", first)
    _insert_txn(conn, uid, "500.00", "expense", "Food", first)
    _insert_txn(conn, uid, "300.00", "expense", "Transport", prev_month_day)

    init_data = _sign(FIELDS)  # FIELDS carries user id 42
    resp = client.get("/app/data", headers={"Authorization": "tma " + init_data})
    conn.rollback()

    assert resp.status_code == 200
    body = resp.text
    assert "₹19,200.00" in body  # all-time balance: 20000 - (500 + 300)
    assert "₹800.00" in body  # all-time expenses
    assert "₹19,500.00" in body  # this month's balance: 20000 - 500
    assert "₹500.00" in body  # this month's expenses (the 300 is last month)
    assert "this week" in body  # the week panel (task 99)
    assert "Spending by category" in body  # the category breakdown (task 98)
    assert "width:100.0%" in body  # Food is this month's only expense category

    # The all-time panel carries its own breakdown, which the route builds with a
    # third `month_summary` over a range wide enough to hold any ledger. Last
    # month's Transport is in it and *not* in the month panel — proof the periods
    # are queried separately rather than sharing the month's categories.
    all_panel = body.split('<section class="panel" data-period="all"', 1)[1].split(
        "</section>", 1)[0]
    month_panel = body.split('<section class="panel" data-period="month"', 1)[1].split(
        "</section>", 1)[0]
    assert "Transport" in all_panel and "Food" in all_panel
    assert "Transport" not in month_panel


def test_dashboard_route_month_delta_needs_a_baseline(conn, monkeypatch):
    """The month hero's delta is undefined until a previous month exists, then
    carries the real sign and magnitude (§13, Phase 7).

    Drives the real route + `db.month_summary` against Postgres over the actual
    previous-month range. Seed one month only → "no comparison yet". Seed the
    prior month too → the delta is that month-over-month change, exact.
    """
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))
    uid = get_or_create_user(conn, 42)
    first, _ = current_month_ist()
    init_data = _sign(FIELDS)

    # One month of data only — no previous month to compare against.
    _insert_txn(conn, uid, "500.00", "expense", "Food", first)
    body = client.get("/app/data", headers={"Authorization": "tma " + init_data}).text
    month_panel = body.split('<section class="panel" data-period="month"', 1)[1].split(
        "</section>", 1)[0]
    assert "no comparison yet" in month_panel
    assert "vs last month" not in month_panel  # no fabricated percentage

    # Now seed the previous month: 300 last month, 500 this month → +67%.
    _insert_txn(conn, uid, "300.00", "expense", "Transport", previous_month_first(first))
    body = client.get("/app/data", headers={"Authorization": "tma " + init_data}).text
    conn.rollback()
    month_panel = body.split('<section class="panel" data-period="month"', 1)[1].split(
        "</section>", 1)[0]
    assert "▲ 67% vs last month" in month_panel  # (500-300)/300 = 66.7% → 67


def test_dashboard_route_shows_account_balances(conn, monkeypatch):
    """`GET /app/data` answers "how much do I have" (§18) — before this task
    `account_balances` had exactly one consumer, the weekly reconcile job, and
    nowhere a user could look."""
    from kanakko.db import get_or_create_user, set_account_opening_balance

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    first, _ = current_month_ist()
    _insert_txn(conn, uid, "500.00", "income", "Salary", first)  # mints the household
    set_account_opening_balance(conn, uid, "credit", Decimal("2000"))

    init_data = _sign(FIELDS)
    resp = client.get("/app/data", headers={"Authorization": "tma " + init_data})
    conn.rollback()

    body = resp.text
    accounts_section = body.split('<section class="accounts">', 1)[1].split(
        "</section>", 1)[0]
    assert "Bank" in accounts_section and "₹500.00" in accounts_section
    # A fresh `credit` account owes what was reported, positively (§18's sign
    # convention) — never the negative asset figure the ledger stores it as.
    assert "Card" in accounts_section and "₹2,000.00" in accounts_section
    assert "External" not in accounts_section


def test_dashboard_route_rejects_a_forged_payload(conn, monkeypatch):
    """A payload signed with the wrong token is a 401, not a dashboard (§13)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    forged = _sign(FIELDS, token="999999:attacker-token")
    resp = client.get("/app/data", headers={"Authorization": "tma " + forged})
    conn.rollback()
    assert resp.status_code == 401


def test_dashboard_route_needs_the_authorization_header(monkeypatch):
    """No `tma` header at all is a 401 before any DB work (§13)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert client.get("/app/data").status_code == 401


def test_shell_serves_the_bootstrap_without_a_secret():
    resp = client.get("/app")
    assert resp.status_code == 200
    assert "telegram-web-app.js" in resp.text
    assert TOKEN not in resp.text


def test_shell_body_follows_the_telegram_theme():
    """The page binds its background and text to Telegram's theme vars (§13, task 102).

    Telegram injects `--tg-theme-*` CSS variables; binding the body to them is the
    only mechanism that makes the dashboard dark in a dark theme. Without it the
    body keeps the browser default (black on white) and renders as a glaring white
    panel inside Telegram's dark chrome — the exact bug this task fixes. Assert the
    binding is present (the mechanism), not a rendered pixel colour a headless test
    can't observe.
    """
    css = client.get("/app").text
    assert "var(--tg-theme-bg-color" in css  # page background follows the theme
    assert "var(--tg-theme-text-color" in css  # text colour follows the theme


# --- Recent-transactions list + per-row soft delete (§13, task 100) ---


def test_recent_list_escapes_the_note():
    """A note is user-typed (§11), so it is HTML-escaped before rendering (§13).

    The note is the first user-controlled string the dashboard renders. Without
    `html.escape`, a logged `<script>alert(1)</script>` would be stored XSS in the
    Mini App. Assert the *escaped bytes* are present and the raw `<script>` tag is
    not — not merely that the page "looks fine".
    """
    rows = [(7, Decimal("50.00"), "expense", "Food", "<script>alert(1)</script>", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert "<script>alert(1)</script>" not in out  # not rendered live
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out  # rendered inert


def test_recent_list_renders_row_with_delete_button_and_amount():
    """Each row shows its amount (through `format_amount`, §9) and a delete button
    carrying the `txn_id` the `POST /app/delete` route needs."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert "₹50.00" in out
    assert 'data-id="7"' in out
    assert "Food" in out


def test_recent_list_null_category_is_uncategorised():
    rows = [(9, Decimal("10.00"), "expense", None, "", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert "Uncategorised" in out


def test_recent_list_empty_renders_nothing():
    assert recent_list([]) == ""


def test_recent_list_transfer_shows_accounts_not_a_category_dropdown():
    """A transfer reads as "Bank → SIP" (§18), not as a category-less expense —
    the bug this task fixes. Before the fix, `_category_select` fell through
    `CATEGORIES_BY_TYPE.get("transfer", ())` to an empty, useless dropdown."""
    rows = [(11, Decimal("5000.00"), "transfer", None, "", date(2026, 8, 6), "Bank", "SIP", None)]
    out = recent_list(rows)
    assert "Bank → SIP" in out
    assert "cat-select" not in out  # no category dropdown for a transfer
    assert "Uncategorised" not in out


def test_recent_list_transfer_has_no_income_or_expense_sign():
    """A transfer is neither spending nor income (§18) — no −/+ sign, no income
    tint. Both directions are asserted so an unconditional sign can't sneak by."""
    rows = [(11, Decimal("5000.00"), "transfer", None, "", date(2026, 8, 6), "Bank", "SIP", None)]
    out = recent_list(rows)
    assert "₹5,000.00" in out
    assert "−₹5,000.00" not in out
    assert "+₹5,000.00" not in out
    assert 'class="amt in"' not in out  # not tinted like income


def _fresh_init_data(user_id: int = 42) -> str:
    """A valid `initData` whose `auth_date` is now — passes the delete route's
    24h freshness window. Signed for `user_id`."""
    now_ts = str(int(datetime.now(timezone.utc).timestamp()))
    return _sign({"auth_date": now_ts, "user": f'{{"id":{user_id}}}'})


def test_delete_route_soft_deletes_the_users_row(conn, monkeypatch):
    """`POST /app/delete` soft-deletes the named row and it drops out of the
    dashboard's recent list and totals (§6, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))
    txn_id = _insert_txn(conn, uid, "300.00", "expense", "Transport", date(2026, 8, 6))

    resp = client.post(
        "/app/delete",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == 0  # gone from the view
    conn.rollback()


def test_delete_route_cannot_delete_another_users_row(conn, monkeypatch):
    """A row belonging to a different user is a 404, never deleted — the delete is
    scoped to the signed user id (§1, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    get_or_create_user(conn, 42)  # the caller must be admitted; the fix 403s an unknown one
    other = get_or_create_user(conn, 99)  # not user 42, whom the initData names
    txn_id = _insert_txn(conn, other, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/delete",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=42)},
        json={"id": txn_id},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == 1  # still live — the other user's row untouched
    conn.rollback()


def test_delete_route_logs_the_money_mutation(conn, monkeypatch):
    """The dashboard delete emits one §17 `transaction.deleted` line carrying the
    amount, `source="miniapp"` and no `update_id` (an HTTP route is not a Telegram
    update, gap 2); a 404 (another user's row) is `status="noop"` with no amount —
    the reason `noop` exists. Both directions asserted: a scrubber-passes-everything
    line and an over-eager `ok` would each be caught."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    other = get_or_create_user(conn, 99)
    mine = _insert_txn(conn, uid, "300.00", "expense", "Transport", date(2026, 8, 6))
    theirs = _insert_txn(conn, other, "500.00", "expense", "Food", date(2026, 8, 6))

    events = []
    eventlog.bind_sink(events.append)
    try:
        ok = client.post("/app/delete", headers={"Authorization": "tma " + _fresh_init_data()},
                         json={"id": mine})
        miss = client.post("/app/delete", headers={"Authorization": "tma " + _fresh_init_data()},
                           json={"id": theirs})
    finally:
        eventlog.unbind_sink()

    assert (ok.status_code, miss.status_code) == (204, 404)
    assert [(e["event"], e["status"]) for e in events] == [
        ("transaction.deleted", "ok"),
        ("transaction.deleted", "noop"),
    ]
    assert events[0]["source"] == "miniapp" and "update_id" not in events[0]
    assert events[0]["txn_id"] == mine and events[0]["amount"] == Decimal("300.00")
    assert "amount" not in events[1]  # a noop touched no row
    conn.rollback()


# --- Per-row category change from the dashboard (§13, task 101) ---


def test_recent_list_renders_a_category_select():
    """Each row carries a `<select>` of the type's categories, current one selected,
    naming the `txn_id` the `POST /app/category` route needs (§5, §13)."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert 'class="cat-select" data-id="7"' in out
    assert "<option selected>Food</option>" in out
    assert "<option>Transport</option>" in out  # another expense option offered
    assert "Salary" not in out  # income-only category not offered on an expense


def test_recent_list_null_category_select_defaults_to_uncategorised():
    rows = [(9, Decimal("10.00"), "expense", None, "", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert '<option value="" disabled selected>Uncategorised</option>' in out


def test_category_route_changes_the_users_row(conn, monkeypatch):
    """`POST /app/category` relabels the named row; the new category is what the
    dashboard then reads back through the view (§5, §6, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/category",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "category": "Transport"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT category FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == "Transport"
    conn.rollback()


def test_category_route_logs_the_money_mutation(conn, monkeypatch):
    """The dashboard recategorise emits one §17 `transaction.recategorised` line —
    `source="miniapp"`, no `update_id`, and the amount on the `ok` path; a 404
    (another user's row) is `noop` with no amount."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    other = get_or_create_user(conn, 99)
    mine = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))
    theirs = _insert_txn(conn, other, "70.00", "expense", "Food", date(2026, 8, 6))

    events = []
    eventlog.bind_sink(events.append)
    try:
        ok = client.post("/app/category", headers={"Authorization": "tma " + _fresh_init_data()},
                         json={"id": mine, "category": "Transport"})
        miss = client.post("/app/category", headers={"Authorization": "tma " + _fresh_init_data()},
                           json={"id": theirs, "category": "Transport"})
    finally:
        eventlog.unbind_sink()

    assert (ok.status_code, miss.status_code) == (204, 404)
    assert [(e["event"], e["status"]) for e in events] == [
        ("transaction.recategorised", "ok"),
        ("transaction.recategorised", "noop"),
    ]
    assert events[0]["source"] == "miniapp" and "update_id" not in events[0]
    assert events[0]["txn_id"] == mine and events[0]["amount"] == Decimal("500.00")
    assert "amount" not in events[1]
    conn.rollback()


def test_category_route_rejects_an_unknown_category(conn, monkeypatch):
    """A category outside the closed set (`categories.py`, §11) is a 400 and writes
    nothing — a forged body can't put a junk label on the ledger."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/category",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "category": "Bribes"},
    )
    assert resp.status_code == 400
    with conn.cursor() as cur:
        cur.execute("SELECT category FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == "Food"  # unchanged
    conn.rollback()


def test_category_route_cannot_change_another_users_row(conn, monkeypatch):
    """A row belonging to a different user is a 404, never relabeled — scoped to the
    signed user id (§1, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    get_or_create_user(conn, 42)  # the caller must be admitted; the fix 403s an unknown one
    other = get_or_create_user(conn, 99)  # not user 42, whom the initData names
    txn_id = _insert_txn(conn, other, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/category",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=42)},
        json={"id": txn_id, "category": "Transport"},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT category FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == "Food"  # unchanged
    conn.rollback()


def test_category_route_rejects_a_stale_init_data(conn, monkeypatch):
    """A captured `initData` older than 24h can't relabel a row — the mutation route
    passes `max_age`, so a valid-but-stale HMAC is 401, not an edit (§13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    stale = _sign({"auth_date": "1700000000", "user": '{"id":42}'})  # 2023
    resp = client.post(
        "/app/category",
        headers={"Authorization": "tma " + stale},
        json={"id": txn_id, "category": "Transport"},
    )
    assert resp.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT category FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == "Food"  # unchanged
    conn.rollback()


# --- Per-row field edit from the dashboard: amount, date, note, account (§13, §18, task 974) ---


def test_recent_list_renders_a_labelled_editor_panel_reached_by_tapping_the_row():
    """The row editor is now reached by tapping the collapsed row (`.txn-head`),
    not a separate ✎ glyph button (task 1704) — every field carries a visible
    text label, not just an `aria-label`, and appears in consequence order
    (§5: category is the most-often-wrong field) — Amount, Category, Date,
    Account, Note — current values pre-filled."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 3)]
    accounts = [(3, "Bank"), (4, "Wallet")]
    out = recent_list(rows, accounts)
    assert 'class="txn-head" data-id="7"' in out
    assert 'class="txn-panel" hidden data-id="7"' in out
    assert "edit-toggle" not in out  # the old separate toggle button is gone
    order = [out.index(marker) for marker in (
        "Amount (₹)</span>", "Category</span>", "Date</span>", "Account</span>", "Note</span>",
    )]
    assert order == sorted(order)
    assert 'class="edit-date" data-id="7" data-edit-field="occurred_on"' in out
    assert 'value="2026-08-06"' in out
    assert "06 Aug 2026" in out  # human-readable date beside the native input
    assert 'class="edit-amount" data-id="7" data-edit-field="amount"' in out
    assert 'value="50.00"' in out
    assert 'class="edit-note" data-id="7" data-edit-field="note"' in out
    assert 'value="lunch"' in out
    assert 'class="edit-account" data-id="7" data-edit-field="account_id"' in out
    assert '<option value="3" selected>Bank</option>' in out
    assert '<option value="4">Wallet</option>' in out


def test_recent_list_header_shows_category_as_text_not_a_dropdown():
    """The collapsed row is data only — the category `<select>` now lives inside
    the hidden panel (task 1704). Before this, the picker's own chevron sat
    beside a row that also expands, two chevron-ish affordances for one
    meaning."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    head, _, panel = out.partition('class="txn-panel"')
    assert "Food" in head
    assert "cat-select" not in head
    assert "cat-select" in panel


def test_recent_list_delete_and_refund_are_labelled_and_only_inside_the_panel():
    """Delete and Refund are words, not glyphs, and reachable only once the row
    is expanded (task 1704) — deleting a row is now two deliberate taps
    (expand, then Delete), not one tap on a ✕ that read as "close" on a card
    that also expands. Reddens if either button reappears in the always-visible
    collapsed part of the row, which is the bug that made every expense row at
    least 132px tall."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    head, _, panel = out.partition('class="txn-panel"')
    assert 'class="del"' not in head and 'class="refund-toggle"' not in head
    assert 'class="del" data-id="7"' in panel and ">Delete<" in panel
    assert 'class="refund-toggle" data-id="7"' in panel and ">Refund<" in panel


def test_recent_list_head_button_carries_aria_expanded():
    """`aria-expanded` names the row's own state so a screen reader announces
    whether tapping it opens or closes the panel; `shell.py`'s click handler
    flips it alongside `.txn-panel[hidden]`."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert 'aria-expanded="false"' in out


def test_recent_list_edit_panel_hides_account_select_for_a_single_account():
    """A household with one account gets no account `<select>` in the row editor
    — a single-option dropdown is a dead control, not a real choice. Mirrors
    `confirm.py`'s `show_accounts = bool(accounts) and len(accounts) > 1`
    (§18): the same rule applies wherever an account picker can appear."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 3)]
    accounts = [(3, "Bank")]
    out = recent_list(rows, accounts)
    assert "edit-account" not in out


def test_recent_list_transfer_edit_panel_has_no_account_select():
    """A transfer's amount/date/note are still editable, but it has no single
    account to reassign — it names two ends, not one (§18)."""
    rows = [(11, Decimal("5000.00"), "transfer", None, "", date(2026, 8, 6), "Bank", "SIP", None)]
    accounts = [(3, "Bank"), (4, "SIP")]
    out = recent_list(rows, accounts)
    assert "edit-account" not in out
    assert 'class="edit-amount"' in out
    assert 'class="edit-date"' in out


def test_recent_list_renders_a_refund_toggle_and_panel_for_an_expense():
    """An `expense` row carries a refund toggle and a hidden refund panel naming
    the `txn_id` `POST /app/refund` needs, the amount pre-filled (§13, §18, task
    1082)."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert 'class="refund-toggle" data-id="7"' in out
    assert 'class="txn-refund" hidden data-id="7"' in out
    assert 'class="refund-amount" data-id="7"' in out
    assert 'class="refund-submit" data-id="7"' in out


def test_recent_list_no_refund_control_for_income_or_transfer():
    """Only an `expense` is refundable (§18, task 1082): `create_refund` accepts
    nothing else, so offering the control on income or a transfer would just be a
    tap that always 404s."""
    income = [(8, Decimal("20000.00"), "income", "Salary", "", date(2026, 8, 6), None, None, 1)]
    transfer = [(11, Decimal("5000.00"), "transfer", None, "", date(2026, 8, 6), "Bank", "SIP", None)]
    assert "refund-toggle" not in recent_list(income)
    assert "refund-toggle" not in recent_list(transfer)


def test_recurring_list_renders_a_rule_with_pause_and_delete():
    """An active rule renders its fields, a pause toggle carrying `data-active`
    for the state it is *in* (§13, task 1161 split 2/2a)."""
    rules = [{"rule_id": 3, "account_id": 1, "account_name": "Bank",
             "category": "Food", "amount": Decimal("5000.00"),
             "day_of_month": 5, "active": True}]
    out = recurring_list(rules)
    assert 'class="rule-toggle" data-id="3" data-active="true"' in out
    assert 'class="rule-del" data-id="3"' in out
    assert "Food" in out and "5,000" in out and "day 5" in out and "Bank" in out
    assert 'class="rule paused"' not in out


def test_recurring_list_paused_rule_shows_resume_and_paused_class():
    """A paused rule is dimmed (`.paused`), not hidden — you must still see it
    to resume it (§13)."""
    rules = [{"rule_id": 4, "account_id": 1, "account_name": "Bank",
             "category": "Food", "amount": Decimal("5000.00"),
             "day_of_month": 5, "active": False}]
    out = recurring_list(rules)
    assert 'class="rule paused"' in out
    assert 'data-active="false"' in out
    assert "Resume" in out


def test_recurring_list_empty_renders_nothing():
    assert recurring_list([]) == ""


def test_account_balances_section_renders_a_row_per_account_and_hides_external():
    """"How much do I have?" (§18) — one row per live account, credit already
    signed as what is owed by `account_balances`'s own query, `external` never
    shown (structural, same rule every other account list follows)."""
    balances = [
        {"account_id": 1, "name": "Bank", "kind": "spending",
         "is_default": True, "balance": Decimal("15000.00")},
        {"account_id": 2, "name": "Card", "kind": "credit",
         "is_default": False, "balance": Decimal("2000.00")},
        {"account_id": 3, "name": "External", "kind": "external",
         "is_default": False, "balance": Decimal("0.00")},
    ]
    out = account_balances_section(balances)
    assert "Bank" in out and "₹15,000.00" in out
    assert "Card" in out and "₹2,000.00" in out
    assert "External" not in out


def test_account_balances_section_empty_renders_nothing():
    assert account_balances_section([]) == ""


def _insert_rule(conn, user_id, account_id, category="Food", amount="5000.00", day=5):
    from kanakko.db import create_recurring_rule
    return create_recurring_rule(conn, user_id, account_id, category, Decimal(amount), day)


def test_recurring_active_route_pauses_and_is_household_scoped(conn, monkeypatch):
    """`POST /app/recurring/active` calls the real `set_recurring_rule_active` —
    any household member may pause a rule another member created (§16, §18)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))

    owner = get_or_create_user(conn, 42)
    hh = household_of(conn, owner)
    member = get_or_create_user(conn, 99)
    join_household(conn, hh, member)
    acc = default_account_of(conn, hh)
    rule = _insert_rule(conn, owner, acc)

    resp = client.post(
        "/app/recurring/active",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=99)},
        json={"id": rule["rule_id"], "active": False},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM recurring_rules WHERE rule_id = %s", (rule["rule_id"],))
        assert cur.fetchone() == (False,)
    conn.rollback()


def test_recurring_active_route_foreign_household_is_404(conn, monkeypatch):
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))

    owner = get_or_create_user(conn, 42)
    hh = household_of(conn, owner)
    acc = default_account_of(conn, hh)
    rule = _insert_rule(conn, owner, acc)

    stranger = get_or_create_user(conn, 99)
    household_of(conn, stranger)  # a household of their own, not owner's

    resp = client.post(
        "/app/recurring/active",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=99)},
        json={"id": rule["rule_id"], "active": False},
    )
    conn.rollback()
    assert resp.status_code == 404


def test_recurring_active_route_rejects_a_bad_body(conn, monkeypatch):
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))
    get_or_create_user(conn, 42)
    headers = {"Authorization": "tma " + _fresh_init_data()}

    assert client.post("/app/recurring/active", headers=headers,
                       json={"active": False}).status_code == 400
    assert client.post("/app/recurring/active", headers=headers,
                       json={"id": 1}).status_code == 400
    # "active" must be an actual bool, not a truthy string — a forged body
    # can't smuggle a non-bool through int()/bool()'s permissive coercion.
    assert client.post("/app/recurring/active", headers=headers,
                       json={"id": 1, "active": "false"}).status_code == 400
    conn.rollback()


def test_recurring_delete_route_deletes_and_is_household_scoped(conn, monkeypatch):
    """`POST /app/recurring/delete` calls the real `delete_recurring_rule` — a
    hard delete, household-scoped like pause/resume (§16, §18)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))

    owner = get_or_create_user(conn, 42)
    hh = household_of(conn, owner)
    member = get_or_create_user(conn, 99)
    join_household(conn, hh, member)
    acc = default_account_of(conn, hh)
    rule = _insert_rule(conn, owner, acc)

    resp = client.post(
        "/app/recurring/delete",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=99)},
        json={"id": rule["rule_id"]},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM recurring_rules WHERE rule_id = %s", (rule["rule_id"],))
        assert cur.fetchone()[0] == 0
    conn.rollback()


def test_recurring_delete_route_missing_is_404(conn, monkeypatch):
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))
    get_or_create_user(conn, 42)

    resp = client.post(
        "/app/recurring/delete",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": 999999},
    )
    conn.rollback()
    assert resp.status_code == 404


def test_recurring_routes_reject_a_stale_init_data(conn, monkeypatch):
    """A genuine-but-old `initData` is a 401 on both recurring routes — they pass
    `max_age` like every other mutation route (§13)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))
    stale = {"Authorization": "tma " + _sign(FIELDS)}  # auth_date = 2023

    active = client.post("/app/recurring/active", headers=stale, json={"id": 1, "active": False})
    delete = client.post("/app/recurring/delete", headers=stale, json={"id": 1})
    conn.rollback()
    assert (active.status_code, delete.status_code) == (401, 401)


def test_recurring_routes_log_the_mutation(conn, monkeypatch):
    """Each route emits one §17 line — `ok` with the rule id on success, `noop`
    (no rule id) on a 404 miss."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(recurring_module, "connect", lambda: _Reuse(conn))

    owner = get_or_create_user(conn, 42)
    hh = household_of(conn, owner)
    acc = default_account_of(conn, hh)
    rule = _insert_rule(conn, owner, acc)
    headers = {"Authorization": "tma " + _fresh_init_data()}

    events = []
    eventlog.bind_sink(events.append)
    try:
        ok = client.post("/app/recurring/active", headers=headers,
                         json={"id": rule["rule_id"], "active": False})
        miss = client.post("/app/recurring/active", headers=headers,
                           json={"id": 999999, "active": False})
    finally:
        eventlog.unbind_sink()

    assert (ok.status_code, miss.status_code) == (204, 404)
    assert [(e["event"], e["status"]) for e in events] == [
        ("recurring_rule.active_set", "ok"),
        ("recurring_rule.active_set", "noop"),
    ]
    assert events[0]["source"] == "miniapp" and "update_id" not in events[0]
    assert events[0]["rule_id"] == rule["rule_id"] and events[0]["active"] is False
    assert "rule_id" not in events[1]
    conn.rollback()


def test_edit_route_changes_the_amount(conn, monkeypatch):
    """`POST /app/edit` with `field: "amount"` updates the row through the real
    `parse_amount` path — never a float (§9)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "amount", "value": "725.50"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == Decimal("725.50")
    conn.rollback()


def test_edit_route_rejects_a_non_positive_amount(conn, monkeypatch):
    """`parse_amount`'s own rule — positive, quantized to paise — is what guards
    this field too; a zero/negative amount is a 400 and writes nothing (§9)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "amount", "value": "-5"},
    )
    assert resp.status_code == 400
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == Decimal("500.00")  # unchanged
    conn.rollback()


def test_edit_route_changes_the_date(conn, monkeypatch):
    """`field: "occurred_on"` moves the row to a new day, which is what a report
    buckets on (§6, §10)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "occurred_on", "value": "2026-08-01"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT occurred_on FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == date(2026, 8, 1)
    conn.rollback()


def test_edit_route_rejects_an_unparsable_date(conn, monkeypatch):
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    from kanakko.db import get_or_create_user

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "occurred_on", "value": "not-a-date"},
    )
    assert resp.status_code == 400
    conn.rollback()


def test_edit_route_changes_and_clears_the_note(conn, monkeypatch):
    """`field: "note"` sets a new note, and an empty value clears it back to
    `NULL` — the dashboard's way to remove a mistaken note (task 974)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6), note="old")

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "note", "value": "new note"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT note FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == "new note"

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "note", "value": "  "},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT note FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] is None  # whitespace-only clears it
    conn.rollback()


def test_edit_route_changes_the_account(conn, monkeypatch):
    """`field: "account_id"` moves an expense to a different one of the
    household's own accounts (§18)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    hh = household_of(conn, uid)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'spending', 'Wallet') RETURNING account_id",
            (hh, uid),
        )
        (wallet_id,) = cur.fetchone()

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "account_id", "value": str(wallet_id)},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == wallet_id
    conn.rollback()


def test_edit_route_rejects_an_account_id_on_a_transfer_row(conn, monkeypatch):
    """A `transfer` names two ends, not one (§18) — an `account_id` edit against
    a transfer row is refused, not silently accepted onto a column the dashboard
    never shows for that row."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    hh = household_of(conn, uid)
    bank = default_account_of(conn, hh)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'locked', 'SIP') RETURNING account_id",
            (hh, uid),
        )
        (sip_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, occurred_on, from_account_id, to_account_id)"
            " VALUES (%s, %s, 5000.00, 'transfer', %s, %s, %s) RETURNING txn_id",
            (uid, hh, date(2026, 8, 6), bank, sip_id),
        )
        (txn_id,) = cur.fetchone()

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "account_id", "value": str(sip_id)},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] is None  # untouched
    conn.rollback()


def test_edit_route_rejects_an_account_id_outside_the_caller_household(conn, monkeypatch):
    """A forged `account_id` naming another household's account (or `external`)
    must not relocate a transaction there (§16, §18) — the dashboard only ever
    offers this household's own, non-`external` accounts, and a tampered request
    is held to the same rule server-side."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    other_uid = get_or_create_user(conn, 99)
    other_hh = household_of(conn, other_uid)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'spending', 'Their Wallet') RETURNING account_id",
            (other_hh, other_uid),
        )
        (their_account,) = cur.fetchone()

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "account_id", "value": str(their_account)},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] != their_account
    conn.rollback()


def test_edit_route_rejects_an_unknown_field(conn, monkeypatch):
    """`field` outside `EDITABLE_TRANSACTION_FIELDS` is a 400 — a forged body
    cannot target an arbitrary column (e.g. `user_id`, `household_id`)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "user_id", "value": "1"},
    )
    assert resp.status_code == 400
    conn.rollback()


def test_edit_route_cannot_change_another_users_row(conn, monkeypatch):
    """A row belonging to a different user is a 404, never edited — scoped to the
    signed user id (§1, §13, §16: "only the member who entered it")."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    get_or_create_user(conn, 42)
    other = get_or_create_user(conn, 99)
    txn_id = _insert_txn(conn, other, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=42)},
        json={"id": txn_id, "field": "amount", "value": "1.00"},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == Decimal("500.00")  # unchanged
    conn.rollback()


def test_edit_route_writes_an_audit_row_with_before_and_after(conn, monkeypatch):
    """Every edit writes one `transaction_events` row, `action='edit'`, with the
    old and new value of the changed field — the audit trail §17 requires for any
    money-changing operation, and the reason migration 013 widened the action
    CHECK. Exercises the real constraint: this fails with a `CheckViolation`
    (surfaced as a 500) if `edit` were ever missing from it again."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "field": "amount", "value": "600.00"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, before, after FROM transaction_events"
            " WHERE txn_id = %s AND action = 'edit'",
            (txn_id,),
        )
        rows = cur.fetchall()
    conn.rollback()
    assert len(rows) == 1
    action, before, after = rows[0]
    assert action == "edit"
    assert before == {"amount": "500.00"}
    assert after == {"amount": "600.00"}


def test_edit_route_logs_the_money_mutation(conn, monkeypatch):
    """The dashboard edit emits one §17 `transaction.edited` line carrying the
    field, `source="miniapp"` and no `update_id`; a 404 (another user's row) is
    `noop` with no amount."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    other = get_or_create_user(conn, 99)
    mine = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))
    theirs = _insert_txn(conn, other, "70.00", "expense", "Food", date(2026, 8, 6))

    events = []
    eventlog.bind_sink(events.append)
    try:
        ok = client.post("/app/edit", headers={"Authorization": "tma " + _fresh_init_data()},
                         json={"id": mine, "field": "amount", "value": "600.00"})
        miss = client.post("/app/edit", headers={"Authorization": "tma " + _fresh_init_data()},
                           json={"id": theirs, "field": "amount", "value": "1.00"})
    finally:
        eventlog.unbind_sink()

    assert (ok.status_code, miss.status_code) == (204, 404)
    assert [(e["event"], e["status"]) for e in events] == [
        ("transaction.edited", "ok"),
        ("transaction.edited", "noop"),
    ]
    assert events[0]["source"] == "miniapp" and "update_id" not in events[0]
    assert events[0]["field"] == "amount"
    assert events[0]["txn_id"] == mine and events[0]["amount"] == Decimal("600.00")
    assert "amount" not in events[1]
    conn.rollback()


def test_edit_route_rejects_a_stale_init_data(conn, monkeypatch):
    """A captured `initData` older than 24h can't edit a row — the mutation route
    passes `max_age`, so a valid-but-stale HMAC is 401, not an edit (§13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))

    stale = _sign({"auth_date": "1700000000", "user": '{"id":42}'})  # 2023
    resp = client.post(
        "/app/edit",
        headers={"Authorization": "tma " + stale},
        json={"id": txn_id, "field": "amount", "value": "1.00"},
    )
    assert resp.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == Decimal("500.00")  # unchanged
    conn.rollback()


def test_delete_route_rejects_a_stale_init_data(conn, monkeypatch):
    """A genuine-but-old `initData` is a 401 on the mutation route — proof the
    route passes `max_age` (§13, task 100). `FIELDS` carries a 2023 `auth_date`,
    far outside the 24h window, yet its HMAC is valid. Without the freshness
    guard a captured payload would be a delete button that works forever."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    resp = client.post(
        "/app/delete",
        headers={"Authorization": "tma " + _sign(FIELDS)},  # auth_date = 2023
        json={"id": 1},
    )
    conn.rollback()
    assert resp.status_code == 401


# --- Undo a delete from the dashboard toast (§6, §13, task 1771) ---


def test_recent_list_delete_button_carries_the_amount_for_the_undo_toast():
    """`.del` carries `data-amount` so the client can build "Deleted ₹500.00 ·
    Undo" without parsing the button's `aria-label` — a separate string with a
    different shape ("Delete −₹500.00 on 16 Aug")."""
    rows = [(7, Decimal("500.00"), "expense", "Food", "", date(2026, 8, 6), None, None, 1)]
    out = recent_list(rows)
    assert 'data-amount="₹500.00"' in out


def test_restore_route_undeletes_the_users_row(conn, monkeypatch):
    """`POST /app/restore` clears `deleted_at` and the row is live again through
    the view the dashboard reads (§6, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))
    delete = client.post("/app/delete", headers={"Authorization": "tma " + _fresh_init_data()},
                         json={"id": txn_id})
    assert delete.status_code == 204

    resp = client.post(
        "/app/restore",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == 1  # back in the view
    conn.rollback()


def test_restore_route_cannot_undelete_another_users_row(conn, monkeypatch):
    """A deleted row belonging to a different user is a 404, never restored —
    scoped to the signed user id, the same rule `/app/delete` follows (§1, §13)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    get_or_create_user(conn, 42)
    other = get_or_create_user(conn, 99)
    txn_id = _insert_txn(conn, other, "500.00", "expense", "Food", date(2026, 8, 6))
    with conn.cursor() as cur:
        cur.execute("UPDATE transactions SET deleted_at = now() WHERE txn_id = %s", (txn_id,))

    resp = client.post(
        "/app/restore",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=42)},
        json={"id": txn_id},
    )
    assert resp.status_code == 404
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == 0  # still deleted — the other user's row untouched
    conn.rollback()


def test_restore_route_on_a_live_row_is_404(conn, monkeypatch):
    """Restoring an id that was never deleted (a stale/replayed Undo tap, or one
    fired twice) is a 404, not a silent no-op that could be mistaken for success."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_txn(conn, uid, "500.00", "expense", "Food", date(2026, 8, 6))  # never deleted

    resp = client.post(
        "/app/restore",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id},
    )
    assert resp.status_code == 404
    conn.rollback()


def test_restore_route_rejects_a_stale_init_data(conn, monkeypatch):
    """Like `/app/delete`, `/app/restore` passes `max_age` — a captured Undo tap
    must not stay live forever (§13)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    resp = client.post(
        "/app/restore",
        headers={"Authorization": "tma " + _sign(FIELDS)},  # auth_date = 2023
        json={"id": 1},
    )
    conn.rollback()
    assert resp.status_code == 401


# --- Refund from the dashboard row (§13, §16, §18, task 1082) ---


def _insert_expense(conn, user_id, amount, occurred_on=date(2026, 8, 6)):
    return _insert_txn(conn, user_id, amount, "expense", "Food", occurred_on)


def test_refund_route_creates_the_refund(conn, monkeypatch):
    """`POST /app/refund` writes a linked refund row through the real
    `create_refund` — the same write the bot's chooser uses (§18)."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_expense(conn, uid, "500.00")

    resp = client.post(
        "/app/refund",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "amount": "200.00"},
    )
    assert resp.status_code == 204
    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, refund_of_txn_id FROM active_transactions"
            " WHERE type = 'refund' AND refund_of_txn_id = %s", (txn_id,))
        assert cur.fetchone() == (Decimal("200.00"), txn_id)
    conn.rollback()


def test_refund_route_is_household_scoped_not_user_scoped(conn, monkeypatch):
    """§16: any member may refund any member's expense — unlike `/app/edit`,
    which restricts to whoever entered the row."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    owner = get_or_create_user(conn, 42)
    hh = household_of(conn, owner)
    member = get_or_create_user(conn, 99)
    join_household(conn, hh, member)
    txn_id = _insert_expense(conn, owner, "500.00")

    resp = client.post(
        "/app/refund",
        headers={"Authorization": "tma " + _fresh_init_data(user_id=99)},
        json={"id": txn_id, "amount": "500.00"},
    )
    assert resp.status_code == 204
    conn.rollback()


def test_refund_route_over_limit_is_409_and_writes_nothing(conn, monkeypatch):
    """The trigger's `RaiseException` on an over-limit refund is answered 409, not
    500 — the same guard the bot side relies on (§18, migration 014). Verified by
    causing the exact overshoot the trigger exists to catch."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_expense(conn, uid, "500.00")

    resp = client.post(
        "/app/refund",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": txn_id, "amount": "600.00"},
    )
    assert resp.status_code == 409
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM active_transactions WHERE refund_of_txn_id = %s", (txn_id,))
        assert cur.fetchone()[0] == 0  # nothing stored
    conn.rollback()


def test_refund_route_gone_expense_is_404(conn, monkeypatch):
    """A missing, foreign-household, or non-expense `id` is a 404 — `create_refund`
    returns `None` rather than writing anything (§18)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    from kanakko.db import get_or_create_user
    get_or_create_user(conn, 42)

    resp = client.post(
        "/app/refund",
        headers={"Authorization": "tma " + _fresh_init_data()},
        json={"id": 999999, "amount": "10.00"},
    )
    conn.rollback()
    assert resp.status_code == 404


def test_refund_route_rejects_a_bad_body(conn, monkeypatch):
    """A missing `id`/`amount`, or an amount that doesn't parse, is a 400 — a
    forged body can't reach `create_refund` at all (§9)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    from kanakko.db import get_or_create_user
    get_or_create_user(conn, 42)
    headers = {"Authorization": "tma " + _fresh_init_data()}

    assert client.post("/app/refund", headers=headers, json={"amount": "10.00"}).status_code == 400
    assert client.post("/app/refund", headers=headers, json={"id": 1}).status_code == 400
    assert client.post("/app/refund", headers=headers,
                       json={"id": 1, "amount": "not-a-number"}).status_code == 400
    conn.rollback()


def test_refund_route_rejects_a_stale_init_data(conn, monkeypatch):
    """A genuine-but-old `initData` is a 401 on the refund route too — it passes
    `max_age` like every other mutation route (§13)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    resp = client.post(
        "/app/refund",
        headers={"Authorization": "tma " + _sign(FIELDS)},  # auth_date = 2023
        json={"id": 1, "amount": "10.00"},
    )
    conn.rollback()
    assert resp.status_code == 401


def test_refund_route_logs_the_money_mutation(conn, monkeypatch):
    """The dashboard refund emits one §17 `refund.created` line — `source="miniapp"`,
    no `update_id`, the amount on `ok`; an over-limit tap is `noop` with
    `outcome="over_limit"` and no amount."""
    from kanakko.db import get_or_create_user

    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    uid = get_or_create_user(conn, 42)
    txn_id = _insert_expense(conn, uid, "500.00")

    events = []
    eventlog.bind_sink(events.append)
    try:
        ok = client.post("/app/refund", headers={"Authorization": "tma " + _fresh_init_data()},
                         json={"id": txn_id, "amount": "200.00"})
        over = client.post("/app/refund", headers={"Authorization": "tma " + _fresh_init_data()},
                           json={"id": txn_id, "amount": "301.00"})
    finally:
        eventlog.unbind_sink()

    assert (ok.status_code, over.status_code) == (204, 409)
    assert [(e["event"], e["status"]) for e in events] == [
        ("refund.created", "ok"),
        ("refund.created", "noop"),
    ]
    assert events[0]["source"] == "miniapp" and "update_id" not in events[0]
    assert events[0]["amount"] == Decimal("200.00")
    assert events[1]["outcome"] == "over_limit" and "amount" not in events[1]
    conn.rollback()


def test_collapsed_row_height_no_longer_depends_on_which_actions_it_has():
    """Before task 1704, `.del`/`.edit-toggle`/`.refund-toggle` were each pinned to
    their own 44px grid row unconditionally, so every expense row was at least
    132px tall for content needing 44-64px, whether or not it carried a note.

    What this catches, and what it can't. A rendered-pixel-height comparison is a
    rendered-layout fact: no headless assertion can see it (verified by rendering
    the real markup in Chrome at 390px in both themes, before and after). So this
    guard pins the *mechanism* instead — the always-visible part of a row is one
    button (`.txn-head`) with a single bounded `min-height`, and no CSS rule gives
    an action button a fixed `grid-row` lane any more, which is what used to force
    the height regardless of content. Reddens if that fixed-lane CSS returns.
    """
    assert "grid-row" not in SHELL_HTML
    head_rule = SHELL_HTML.split("\n.txn-head {", 1)[1].split("}", 1)[0]
    assert "min-height: 44px" in head_rule
    assert "height:" not in head_rule.replace("min-height:", "")  # bounded, not fixed

    note = recent_list([(1, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 1)])
    no_note = recent_list([(2, Decimal("50.00"), "expense", "Food", "", date(2026, 8, 6), None, None, 1)])
    # Neither collapsed row carries an action button — both are data-only, so
    # neither pays the old fixed-lane height regardless of the note.
    for out in (note.split('class="txn-panel"')[0], no_note.split('class="txn-panel"')[0]):
        assert 'class="del"' not in out and 'class="refund-toggle"' not in out


def test_shell_reloads_when_the_mini_app_is_reopened():
    """A restored Mini App refetches, rather than showing what it rendered on open.

    The bug: `load()` ran once at startup and nothing rebound it, so logging an
    expense in the chat while the dashboard was minimized left the figures stale
    until the app was fully closed and reopened. A finance dashboard quietly
    disagreeing with its own ledger is the failure mode worth guarding.

    Both listeners are asserted because they cover different clients: Telegram's
    `activated` fires "when the Mini App becomes active (e.g., opened from
    minimized state)" but only on Bot API 8.0+, and `visibilitychange` is the
    plain-web fallback for older clients. Dropping either one leaves a real
    surface stale, so neither is redundant. This asserts the wiring — whether a
    given Telegram build actually emits the event can only be seen on a device.
    """
    assert "tg.onEvent('activated', load)" in SHELL_HTML
    # Anchor on the call, not the bare word: `visibilitychange` appears in the
    # comment above the listener too, and splitting on that matched the prose.
    call = "document.addEventListener('visibilitychange'"
    assert call in SHELL_HTML
    # and the handler must actually reload, not merely be registered
    listener = SHELL_HTML.split(call, 1)[1].split("\n", 1)[0]
    assert "load()" in listener


def test_a_failed_mutation_is_surfaced_not_swallowed():
    """Every mutating call goes through one helper that always reloads and only
    toasts on failure (task 1558) — not the old `.then(r => { if (r.ok) load(); })`.

    The bug: six inlined call sites (edit, delete, refund, category, recurring
    pause, recurring delete) only called `load()` when the response was `ok`, so
    a 4xx/5xx or a dropped connection did *nothing* — the field kept showing what
    the user typed, and the next `load()` (which fires on every re-activation)
    silently reverted it with no sign anything had gone wrong.

    This pins the two halves of the fix, not just that a `mutate` name exists:
    `load()` is unconditional (in `.finally`, not gated on `r.ok`) and the toast
    fires exactly on failure (`!r.ok` and the network-error `.catch`). A version
    that renamed the old bug (e.g. `.then(r => { if (r.ok) load(); else showToast() })`,
    which still skips the reload on failure) would still fail this.
    """

    def body_of(fn_name: str) -> str:
        start = SHELL_HTML.index(f"function {fn_name}(")
        # Balance braces from the first `{` after the signature to find the
        # matching close, since the body itself contains nested `{ ... }`.
        brace_start = SHELL_HTML.index("{", start)
        depth = 0
        for i in range(brace_start, len(SHELL_HTML)):
            if SHELL_HTML[i] == "{":
                depth += 1
            elif SHELL_HTML[i] == "}":
                depth -= 1
                if depth == 0:
                    return SHELL_HTML[brace_start : i + 1]
        raise AssertionError(f"unbalanced braces in function {fn_name}")

    mutate = body_of("mutate")
    assert ".then(r => { if (!r.ok) { showToast(); return; } if (onSuccess) onSuccess(); })" in mutate
    assert ".catch(showToast)" in mutate, "a thrown/network error must toast too"
    assert ".finally(load)" in mutate, "load() must run unconditionally"
    # load() must not be inside the `.then(...)` success branch — it has to run
    # whether or not the request succeeded, so the only `load` in the whole
    # function body is the one in `.finally`.
    assert mutate.count("load") == 1

    # None of the old inlined, success-only reloads survive at any call site.
    assert "if (r.ok) load()" not in SHELL_HTML

    # Every one of the seven mutating actions routes through the helper, not a
    # bespoke fetch.
    for route in (
        "/app/refund",
        "/app/recurring/active",
        "/app/recurring/delete",
        "/app/delete",
        "/app/restore",
        "/app/category",
        "/app/edit",
    ):
        assert f"mutate('{route}'," in SHELL_HTML, f"{route} bypasses mutate()"


def test_delete_offers_undo_instead_of_confirming_first():
    """The dashboard doesn't ask "are you sure?" before a delete — it deletes,
    then offers Undo (task 1771). A `confirm(...)` dialog is the one shape this
    task explicitly rejects: it taxes the (likelier) case where the user meant
    it, where an undo toast doesn't."""
    assert "confirm(" not in SHELL_HTML

    def body_of(fn_name: str) -> str:
        start = SHELL_HTML.index(f"function {fn_name}(")
        brace_start = SHELL_HTML.index("{", start)
        depth = 0
        for i in range(brace_start, len(SHELL_HTML)):
            if SHELL_HTML[i] == "{":
                depth += 1
            elif SHELL_HTML[i] == "}":
                depth -= 1
                if depth == 0:
                    return SHELL_HTML[brace_start : i + 1]
        raise AssertionError(f"unbalanced braces in function {fn_name}")

    undo = body_of("showUndoToast")
    assert "Undo" in undo
    assert "toast-undo" in undo
    # The delete call site passes the toast as an onSuccess callback, not a
    # bespoke .then() — the shape that would let a delete skip the toast.
    assert "mutate('/app/delete', {id}, () => showUndoToast(id, btn.dataset.amount));" in SHELL_HTML
    # The toast's own Undo button restores through the same mutate() helper,
    # so a failed restore still toasts and still reloads.
    assert "mutate('/app/restore', {id: Number(undo.dataset.id)});" in SHELL_HTML


def test_mini_app_refuses_a_user_who_was_never_admitted(conn, monkeypatch):
    """A valid `initData` from an unadmitted user gets 403 and mints no row (§16).

    `authenticated_user` proves *which* Telegram user is asking, never that they
    are permitted. The routes used to call `get_or_create_user`, so anyone who
    found the bot and tapped the menu button minted a `users` row — and that row
    then satisfied the bot's own gate, which asks `user_exists`. The Mini App was
    a way around the invite gate.

    Both halves are asserted because either alone passes on a broken fix: a 403
    that still created the row would leave the bypass in place, and no-row with a
    200 would leak an empty dashboard. The `users` count is taken before and after
    so the assertion is about *this* request, not the table being empty.
    """
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = 42")
        (before,) = cur.fetchone()
    assert before == 0, "FIELDS' user 42 must be unknown for this to prove anything"

    init_data = _sign(FIELDS)  # FIELDS carries user id 42 — never admitted
    resp = client.get("/app/data", headers={"Authorization": "tma " + init_data})

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = 42")
        (after,) = cur.fetchone()
    conn.rollback()

    assert resp.status_code == 403
    assert after == 0, "the Mini App must not mint a user row for an unadmitted caller"


def test_mini_app_mutations_refuse_an_unadmitted_user(conn, monkeypatch):
    """The four mutating routes reject an unadmitted caller too (§16).

    The read route is the one that minted the row, but a fix applied only there
    would leave `/app/delete`, `/app/category`, `/app/edit`, and `/app/refund`
    creating users. They pass a 24h `max_age`, so the payload is signed fresh.
    """
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))
    monkeypatch.setattr(refund_module, "connect", lambda: _Reuse(conn))

    fresh = dict(FIELDS, auth_date=str(int(datetime.now(timezone.utc).timestamp())))
    init_data = _sign(fresh)
    headers = {"Authorization": "tma " + init_data}

    delete = client.post("/app/delete", json={"id": 1}, headers=headers)
    category = client.post("/app/category", json={"id": 1, "category": "Food"},
                           headers=headers)
    edit = client.post("/app/edit", json={"id": 1, "field": "note", "value": "x"},
                       headers=headers)
    refund = client.post("/app/refund", json={"id": 1, "amount": "10.00"},
                         headers=headers)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = 42")
        (after,) = cur.fetchone()
    conn.rollback()

    assert delete.status_code == 403
    assert category.status_code == 403
    assert edit.status_code == 403
    assert refund.status_code == 403
    assert after == 0
