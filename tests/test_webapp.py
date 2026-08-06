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
from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko.app import app
from kanakko.migrate import migrate
from kanakko.jobs.evening import IST
from kanakko.webapp import (
    InitDataError,
    category_bars,
    current_month_ist,
    current_week_ist,
    dashboard_html,
    user_id_from_init_data,
    validate_init_data,
)

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


def test_dashboard_html_shows_rupee_amounts_and_exact_balance():
    """Balance is exact `Decimal` subtraction — the money invariant (§9)."""
    html = dashboard_html(
        Decimal("20000.00"), Decimal("500.50"),
        Decimal("0.70"), Decimal("0.50"),
        "August 2026",
        Decimal("0.30"), Decimal("0.10"), [],
    )
    assert "₹20,000.00" in html  # all-time income
    assert "₹500.50" in html  # all-time expenses
    assert "₹19,499.50" in html  # all-time balance, to the paise
    assert "₹0.20" in html  # this week's balance: 0.70 - 0.50, no float drift
    assert "This week" in html
    assert "August 2026" in html


def test_dashboard_html_week_and_month_balances_are_distinct():
    """The week and month summaries render their own balances, not a shared one (§13)."""
    html = dashboard_html(
        Decimal("100"), Decimal("40"),
        Decimal("30"), Decimal("10"),  # week: balance 20
        "August 2026",
        Decimal("80"), Decimal("25"), [],  # month: balance 55
    )
    assert "This week" in html
    assert "₹20.00" in html  # week balance: 30 - 10
    assert "₹55.00" in html  # month balance: 80 - 25


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
    html = dashboard_html(
        Decimal("1000"), Decimal("400"),
        Decimal("500"), Decimal("200"),
        "August 2026",
        Decimal("1000"), Decimal("400"),
        [("Food", Decimal("400.00"))],
    )
    assert "Spending by category" in html
    assert "width:100.0%" in html  # the sole category is all the spending


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


def _insert_txn(conn, user_id, amount, type_, category, occurred_on):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, amount, type, category, note, occurred_on)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, Decimal(amount), type_, category, "", occurred_on),
        )


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
    assert "This week" in body  # the weekly summary section (task 99)
    assert "Spending by category" in body  # the category breakdown (task 98)
    assert "width:100.0%" in body  # Food is this month's only expense category


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
