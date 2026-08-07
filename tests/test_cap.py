"""The §16 per-user daily message cap: a runaway user cannot run up the bill.

Every inbound message is one LLM call (§2), so §16 caps handled messages per user
per IST day, counted off `processed_updates`. Two things must hold: the count
buckets on IST midnight (§10), not UTC — a 23:50 IST message belongs to that IST
day, not the next it falls into in UTC — and the webhook refuses the message past
the cap *before* the parse, storing nothing and making no OpenRouter call. Reverting
the cap check in the webhook reddens `test_message_past_the_cap_is_refused...`;
reverting the `AT TIME ZONE 'Asia/Kolkata'` bucketing reddens the rollover test.
"""

from datetime import date

from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko import handlers
from kanakko.app import WEBHOOK_SECRET_HEADER, app
from kanakko.db import count_updates_on_day, get_or_create_user
from kanakko.migrate import migrate

client = TestClient(app)

SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}
USER = 555


def _reuse_conn(conn):
    class _Reuse:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    return lambda: _Reuse()


def _claim_at(conn, update_id, user_id, ts):
    """Seed one processed_updates row claimed by `user_id` at instant `ts`."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_updates (update_id, user_id, processed_at)"
            " VALUES (%s, %s, %s)",
            (update_id, user_id, ts),
        )


def _claim_now(conn, update_id, user_id):
    """Seed one processed_updates row claimed by `user_id`, dated now (today IST)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_updates (update_id, user_id) VALUES (%s, %s)",
            (update_id, user_id),
        )


def _text_update(update_id, from_id=USER):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": from_id},
            "from": {"id": from_id},
            "text": "spent 500 on food",
        },
    }


def test_count_buckets_on_ist_midnight_not_utc(conn):
    """A message's IST calendar day is what it counts against, not its UTC day.

    IST is UTC+5:30, so any instant between 18:30 and 24:00 UTC already belongs to
    the *next* IST day. Three rows land on IST Aug 7 — two of them (18:30–24:00 UTC
    on Aug 6) sit on UTC Aug 6 — and one lands on IST Aug 8. Counting by IST gives
    Aug 7 three; counting by the raw UTC date (the bug this guards) would give it
    one. That gap is what makes the assertion fail if the `AT TIME ZONE` is dropped.
    """
    migrate(conn)
    # Force the session TimeZone to UTC — the production risk the explicit
    # `AT TIME ZONE 'Asia/Kolkata'` defends against. Without it a plain
    # `processed_at::date` would bucket by whatever the session TZ happens to be,
    # so this is what makes dropping the `AT TIME ZONE` redden the count below.
    conn.execute("SET TimeZone TO 'UTC'")
    uid = get_or_create_user(conn, USER)
    other = get_or_create_user(conn, USER + 1)

    _claim_at(conn, 1, uid, "2026-08-06 19:00:00+00")  # IST Aug 7 00:30, UTC Aug 6
    _claim_at(conn, 2, uid, "2026-08-06 20:00:00+00")  # IST Aug 7 01:30, UTC Aug 6
    _claim_at(conn, 3, uid, "2026-08-07 10:00:00+00")  # IST Aug 7 15:30, UTC Aug 7
    _claim_at(conn, 4, uid, "2026-08-07 19:00:00+00")  # IST Aug 8 00:30 — next day
    _claim_at(conn, 5, other, "2026-08-07 10:00:00+00")  # another user, not counted

    assert count_updates_on_day(conn, uid, date(2026, 8, 7)) == 3  # UTC bucket → 2
    assert count_updates_on_day(conn, uid, date(2026, 8, 8)) == 1  # the rollover row
    conn.execute("SET TimeZone TO DEFAULT")
    conn.rollback()


def test_message_past_the_cap_is_refused_and_costs_nothing(conn, monkeypatch):
    """At the cap, the next message is refused before the parse and stores nothing.

    Cap 2, two updates already handled today: the third is turned away with
    `CAP_REACHED`, `handle_text` never runs (so no OpenRouter call — the cost the
    cap exists to bound), the update is never claimed, and the endpoint still
    answers 200 so Telegram stops retrying. Reverting the webhook's cap check
    reddens this — the third message would reach `handle_text`.
    """
    migrate(conn)
    uid = get_or_create_user(conn, USER)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setenv("DAILY_MESSAGE_CAP", "2")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    _claim_now(conn, 10, uid)
    _claim_now(conn, 11, uid)

    handled = []
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: handled.append(msg))
    sent = []
    monkeypatch.setattr(app_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    response = client.post("/webhook", json=_text_update(12), headers=AUTH)

    assert response.status_code == 200  # a refusal still answers 200, never a retry
    assert sent == [(USER, handlers.CAP_REACHED)]  # the polite over-limit notice
    assert handled == []  # never reached the parse path — no OpenRouter call
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM processed_updates WHERE update_id = %s", (12,))
        assert cur.fetchone() == (0,)  # the refused update was never claimed
    conn.rollback()


def _confirm_update(update_id, from_id=USER):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cbq",
            "from": {"id": from_id},
            "message": {"message_id": 1, "chat": {"id": from_id}},
            "data": "confirm",
        },
    }


def test_callback_tap_does_not_consume_the_cap(conn, monkeypatch):
    """A free Confirm tap is handled but never metered — §16 counts only LLM calls.

    The cap exists to bound OpenRouter cost, and a Confirm/Cancel/category tap makes
    no LLM call (§2). If a tap were counted, a text→Confirm user would burn two units
    per entry and hit the wall at half the configured budget. This claims a Confirm
    through the real webhook, then asserts the metering count is still zero. Reverting
    the `metered_user = user_id if is_parse else None` guard in the webhook (stamping
    every claim with `user_id`) reddens this: the Confirm row would count.
    """
    migrate(conn)
    uid = get_or_create_user(conn, USER)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))
    monkeypatch.setattr(app_module, "handle_confirm", lambda conn, act: None)

    response = client.post("/webhook", json=_confirm_update(30), headers=AUTH)

    assert response.status_code == 200
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM processed_updates WHERE update_id = %s", (30,))
        assert cur.fetchone() == (1,)  # the tap was claimed (idempotency still holds)
    assert count_updates_on_day(conn, uid, date.today()) == 0  # but not metered
    conn.rollback()


def test_message_under_the_cap_is_served(conn, monkeypatch):
    """One below the cap, the message reaches the parse path — not "refuse everyone".

    Without this the refusal test would pass a cap that turned *every* message
    away. Cap 2, one update handled today: the second is served.
    """
    migrate(conn)
    uid = get_or_create_user(conn, USER)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setenv("DAILY_MESSAGE_CAP", "2")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    _claim_now(conn, 20, uid)

    handled = []
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: handled.append(msg))
    sent = []
    monkeypatch.setattr(app_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    response = client.post("/webhook", json=_text_update(21), headers=AUTH)

    assert response.status_code == 200
    assert len(handled) == 1  # reached the parse path
    assert sent == []  # not refused
    conn.rollback()
