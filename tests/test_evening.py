"""The evening summary job (§12) — day bucketing, money totals, message shape.

The two silent-failure risks here are the ones this project treats as expensive:
bucketing the day in UTC instead of `Asia/Kolkata`, and a total that pulls in a
soft-deleted row by reading `transactions` instead of `active_transactions`.
Each gets a guard that reddens if it regresses. `day_summary` reads real Postgres
(the `conn` fixture); the message and timezone helpers are pure.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from conftest import household_of

from kanakko import eventlog
from kanakko.db import day_summary, get_or_create_user
from kanakko.jobs import DeliveryFailures, evening
from kanakko.jobs.evening import summary_text, today_ist
from kanakko.migrate import migrate


def _insert(conn, user_id, amount, type_, occurred_on, deleted=False):
    deleted_at = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc) if deleted else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, note, occurred_on, deleted_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, household_of(conn, user_id), amount, type_, None, None, occurred_on, deleted_at),
        )


def test_day_summary_buckets_by_ist_date_and_excludes_deleted(conn):
    """Only today's live rows count: yesterday and soft-deleted rows are out.

    A summary read that used `transactions` instead of `active_transactions`
    would fold the deleted ₹500 back into the day's spend; one that bucketed on
    the wrong date would pull in yesterday's ₹99. Both are silent — a wrong total
    errors nowhere — so both are asserted here.
    """
    migrate(conn)
    day = date(2026, 8, 6)
    user = get_or_create_user(conn, 700700)

    _insert(conn, user, Decimal("120.50"), "expense", day)
    _insert(conn, user, Decimal("30.00"), "expense", day)
    _insert(conn, user, Decimal("20000.00"), "income", day)
    _insert(conn, user, Decimal("99.00"), "expense", date(2026, 8, 5))  # yesterday
    _insert(conn, user, Decimal("500.00"), "expense", day, deleted=True)  # soft-deleted

    count, spent, received = day_summary(conn, user, day)

    assert count == 3
    assert spent == Decimal("150.50")
    assert received == Decimal("20000.00")
    assert isinstance(spent, Decimal) and isinstance(received, Decimal)
    conn.rollback()


def test_day_summary_empty_day(conn):
    """A day with no entries is (0, 0.00, 0.00) — the unconditional summary still sends."""
    migrate(conn)
    user = get_or_create_user(conn, 700701)
    count, spent, received = day_summary(conn, user, date(2026, 8, 6))
    assert (count, spent, received) == (0, Decimal("0"), Decimal("0"))
    conn.rollback()


def test_run_logs_an_evening_reminder_for_each_user(conn, monkeypatch):
    """Every user the summary reaches gets a `reminder_log` 'evening' row (§12).

    The noon nudge reads these to place its suppression window, so a job that
    silently skips the write would widen every window — asserted here.
    """
    migrate(conn)
    a = get_or_create_user(conn, 700800)
    b = get_or_create_user(conn, 700801)
    monkeypatch.setattr(evening, "send_message", lambda tg_id, text: None)

    events = []
    eventlog.bind_sink(events.append)
    try:
        sent = evening.run(conn)
    finally:
        eventlog.unbind_sink()

    assert sent == 2
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, kind FROM reminder_log ORDER BY user_id")
        assert cur.fetchall() == [(a, "evening"), (b, "evening")]

    # §17: one `ok` event per run (not per user), carrying the run's shape.
    assert len(events) == 1
    assert events[0]["event"] == "job.evening"
    assert events[0]["status"] == "ok"
    assert events[0]["source"] == "cron"
    assert (events[0]["considered"], events[0]["delivered"], events[0]["skipped"]) == (2, 2, 0)
    assert isinstance(events[0]["duration_ms"], int)
    conn.rollback()


def test_today_ist_buckets_in_kolkata():
    """22:00 UTC is 03:30 IST the next day — the date must roll forward."""
    late = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    assert today_ist(late) == date(2026, 8, 7)
    early = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)  # 15:30 IST, same day
    assert today_ist(early) == date(2026, 8, 6)


def test_summary_text():
    """Income line only when there was income; singular/plural; the empty day."""
    assert summary_text(0, Decimal("0"), Decimal("0")) == "🌙 No entries logged today."
    assert summary_text(1, Decimal("150.50"), Decimal("0")) == (
        "🌙 Today: 1 entry, spent ₹150.50."
    )
    assert summary_text(3, Decimal("150.50"), Decimal("20000")) == (
        "🌙 Today: 3 entries, spent ₹150.50, received ₹20,000.00."
    )


def test_one_blocked_recipient_does_not_silence_the_others(conn, monkeypatch):
    """A failed send for one user must not stop the fan-out, or lose the rest's rows.

    Previously `run` looped without a try, so a single `send_message` raise —
    a user who blocked the bot, one bad chat id — aborted the whole loop, and the
    caller's `with connect()` then rolled back every `reminder_log` row already
    written. Everyone after the bad user got no summary, and everyone before it
    lost the row the noon nudge reads to place its suppression window.

    Three users, the middle one failing. The other two must still be delivered to
    and must still have their rows *after* the failure — which only holds because
    `fan_out` commits before raising. The job still fails loudly (DeliveryFailures)
    so a broken recipient is never silent.
    """
    migrate(conn)
    conn.commit()  # baseline the test's own rows survive the fan-out's commit
    a = get_or_create_user(conn, 700900)
    get_or_create_user(conn, 700901)  # the recipient whose send fails
    c = get_or_create_user(conn, 700902)
    delivered = []

    def flaky_send(telegram_user_id, text):
        if telegram_user_id == 700901:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        delivered.append(telegram_user_id)

    monkeypatch.setattr(evening, "send_message", flaky_send)

    events = []
    eventlog.bind_sink(events.append)
    try:
        with pytest.raises(DeliveryFailures) as caught:
            evening.run(conn)
    finally:
        eventlog.unbind_sink()

    assert delivered == [700900, 700902]  # the blocked user did not stop the rest
    assert caught.value.sent == 2
    assert [tg for tg, _ in caught.value.failures] == [700901]

    # §17: the failure-isolation property already held silently; the log line is
    # what makes it observable. One `status="error"` event, with both halves of
    # the story — the two that succeeded and the one that failed.
    assert len(events) == 1
    assert events[0]["event"] == "job.evening"
    assert events[0]["status"] == "error"
    assert events[0]["source"] == "cron"
    assert events[0]["considered"] == 3
    assert events[0]["delivered"] == 2
    assert events[0]["failed"] == 1

    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM reminder_log ORDER BY user_id")
        rows = [r[0] for r in cur.fetchall()]
    assert rows == [a, c]  # kept, not rolled back; the failed user wrote nothing

    with conn.cursor() as cur:  # this test committed, so clean up after itself
        cur.execute("DELETE FROM reminder_log")
        cur.execute("DELETE FROM users WHERE telegram_user_id IN (700900,700901,700902)")
    conn.commit()
