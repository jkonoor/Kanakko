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
from conftest import default_account_of, household_of
from fastapi.testclient import TestClient

from kanakko import eventlog
from kanakko.app import app
from kanakko.migrate import migrate
from kanakko.webapp import (
    SHELL_HTML,
    InitDataError,
    Period,
    category_bars,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    previous_month_first,
    recent_list,
    user_id_from_init_data,
    validate_init_data,
)
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


def test_recent_list_renders_edit_toggle_and_hidden_panel():
    """Each row carries a hidden per-row editor — date, amount, note, and (for a
    non-transfer row) an account `<select>` naming the `txn_id` `POST /app/edit`
    needs, current values pre-filled (§13, §18)."""
    rows = [(7, Decimal("50.00"), "expense", "Food", "lunch", date(2026, 8, 6), None, None, 3)]
    accounts = [(3, "Bank"), (4, "Wallet")]
    out = recent_list(rows, accounts)
    assert 'class="edit-toggle" data-id="7"' in out
    assert 'class="txn-edit" hidden data-id="7"' in out
    assert 'class="edit-date" data-id="7" data-edit-field="occurred_on"' in out
    assert 'value="2026-08-06"' in out
    assert 'class="edit-amount" data-id="7" data-edit-field="amount"' in out
    assert 'value="50.00"' in out
    assert 'class="edit-note" data-id="7" data-edit-field="note"' in out
    assert 'value="lunch"' in out
    assert 'class="edit-account" data-id="7" data-edit-field="account_id"' in out
    assert '<option value="3" selected>Bank</option>' in out
    assert '<option value="4">Wallet</option>' in out


def test_recent_list_transfer_edit_panel_has_no_account_select():
    """A transfer's amount/date/note are still editable, but it has no single
    account to reassign — it names two ends, not one (§18)."""
    rows = [(11, Decimal("5000.00"), "transfer", None, "", date(2026, 8, 6), "Bank", "SIP", None)]
    accounts = [(3, "Bank"), (4, "SIP")]
    out = recent_list(rows, accounts)
    assert "edit-account" not in out
    assert 'class="edit-amount"' in out
    assert 'class="edit-date"' in out


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


def test_txn_row_places_note_and_delete():
    """A transaction row is a grid: note on its own row, delete pinned to row 1 (§13).

    What this catches, and what it can't. The original CSS made `.txn` a wrapping
    flex row with `.txn-note` at `flex-basis:100%`; because `.del` is a sibling
    *after* the note, it was pushed onto a third line and the row's text collided
    with itself — every row carrying a note was unreadable, while the note-less
    rows looked fine, which is why it survived review. That is a rendered-layout
    bug: no headless assertion can see it. Verified by rendering the real markup
    in Chrome at 390px in both themes, before and after.

    So this guard pins the *mechanism* that fixes it — grid placement — rather
    than claiming to check the appearance. Reverting `.txn` to the wrapping flex
    layout reddens it. Anything subtler than that still needs eyes on a phone.
    """
    def rule(selector: str) -> str:
        """The body of the rule that *starts* a line with `selector`.

        Anchored to the newline on purpose: `.txn-note` also appears in the shared
        `.label, .txn-note { color: ... }` rule, and an unanchored search finds
        that colour declaration instead of the layout one — which is how the first
        version of this test passed the wrong string and failed against correct CSS.
        """
        return SHELL_HTML.split(f"\n{selector} {{", 1)[1].split("}", 1)[0]

    assert "display: grid" in rule(".txn")
    assert "grid-row: 1" in rule(".del")  # delete button stays on the first row
    assert "grid-column: 1" in rule(".txn-note")  # note gets a row of its own


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
    """The three mutating routes reject an unadmitted caller too (§16).

    The read route is the one that minted the row, but a fix applied only there
    would leave `/app/delete`, `/app/category` and `/app/edit` creating users.
    They pass a 24h `max_age`, so the payload is signed fresh.
    """
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "connect", lambda: _Reuse(conn))

    fresh = dict(FIELDS, auth_date=str(int(datetime.now(timezone.utc).timestamp())))
    init_data = _sign(fresh)
    headers = {"Authorization": "tma " + init_data}

    delete = client.post("/app/delete", json={"id": 1}, headers=headers)
    category = client.post("/app/category", json={"id": 1, "category": "Food"},
                           headers=headers)
    edit = client.post("/app/edit", json={"id": 1, "field": "note", "value": "x"},
                       headers=headers)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = 42")
        (after,) = cur.fetchone()
    conn.rollback()

    assert delete.status_code == 403
    assert category.status_code == 403
    assert edit.status_code == 403
    assert after == 0
