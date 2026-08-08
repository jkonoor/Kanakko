"""The noon nudge job (§12) — the suppression window and who gets nudged.

The nudge is only right when the user has logged nothing since the previous
evening summary, so the two things that must not silently break are: the 21:00
`Asia/Kolkata` boundary (bucket it in UTC and you nudge the wrong users), and the
"has any live row since the boundary" read (count a soft-deleted row and you
suppress a nudge the user should have got). Each gets a guard. `logged_since`
reads real Postgres (the `conn` fixture); `previous_evening_ist` is pure.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from conftest import household_of, join_household

from kanakko import eventlog
from kanakko.db import get_or_create_user, logged_since
from kanakko.jobs import noon
from kanakko.jobs.noon import previous_evening_ist, run
from kanakko.migrate import migrate

IST = ZoneInfo("Asia/Kolkata")


def _insert(conn, user_id, created_at, deleted=False):
    deleted_at = datetime(2026, 8, 6, tzinfo=timezone.utc) if deleted else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, note, occurred_on, created_at, deleted_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, household_of(conn, user_id), Decimal("10.00"), "expense", None, None, date(2026, 8, 6), created_at, deleted_at),
        )


def _log_evening(conn, user_id, sent_at):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reminder_log (user_id, kind, sent_at) VALUES (%s, 'evening', %s)",
            (user_id, sent_at),
        )


def _reminder_kinds(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT kind FROM reminder_log WHERE user_id = %s", (user_id,))
        return [kind for (kind,) in cur.fetchall()]


def test_previous_evening_is_last_2100_ist():
    """The boundary is the most recent 21:00 IST — computed in IST, not UTC.

    At noon it's yesterday's 21:00; just after 21:00 it's today's. Bucketing in
    UTC would put the boundary 5.5 hours off and suppress the wrong users.
    """
    noon_today = datetime(2026, 8, 6, 12, 0, tzinfo=IST)
    assert previous_evening_ist(noon_today) == datetime(2026, 8, 5, 21, 0, tzinfo=IST)

    just_after = datetime(2026, 8, 6, 21, 30, tzinfo=IST)
    assert previous_evening_ist(just_after) == datetime(2026, 8, 6, 21, 0, tzinfo=IST)

    # 16:00 UTC on the 6th is 21:30 IST — the boundary must be *today's* 21:00, not
    # yesterday's (which a UTC-date computation off 16:00 would give).
    utc_evening = datetime(2026, 8, 6, 16, 0, tzinfo=timezone.utc)
    assert previous_evening_ist(utc_evening) == datetime(2026, 8, 6, 21, 0, tzinfo=IST)


def test_logged_since_excludes_before_boundary_and_soft_deleted(conn):
    """Only a live row logged at/after the boundary counts as activity.

    A row logged before the boundary is old news; a soft-deleted one was undone.
    Reading `transactions` instead of `active_transactions` would let the deleted
    row keep the nudge suppressed — silently, so it's asserted.
    """
    migrate(conn)
    since = datetime(2026, 8, 5, 21, 0, tzinfo=IST)
    user = get_or_create_user(conn, 800800)

    # Before the boundary → not activity since the summary.
    _insert(conn, user, since - timedelta(hours=1))
    assert logged_since(conn, user, since) is False

    # Live row after the boundary → activity.
    _insert(conn, user, since + timedelta(hours=1))
    assert logged_since(conn, user, since) is True

    conn.rollback()


def test_logged_since_ignores_soft_deleted(conn):
    migrate(conn)
    since = datetime(2026, 8, 5, 21, 0, tzinfo=IST)
    user = get_or_create_user(conn, 800801)
    _insert(conn, user, since + timedelta(hours=1), deleted=True)
    assert logged_since(conn, user, since) is False
    conn.rollback()


def test_run_nudges_only_idle_users(conn, monkeypatch):
    """A user active since the last summary is suppressed; an idle one is nudged."""
    migrate(conn)
    boundary = previous_evening_ist()

    active = get_or_create_user(conn, 900900)
    idle_stale = get_or_create_user(conn, 900901)  # logged, but before the boundary
    get_or_create_user(conn, 900902)  # idle: never logged anything

    _insert(conn, active, boundary + timedelta(minutes=1))
    _insert(conn, idle_stale, boundary - timedelta(hours=1))

    sent_to = []
    monkeypatch.setattr(noon, "send_message", lambda tg_id, text: sent_to.append(tg_id))

    events = []
    eventlog.bind_sink(events.append)
    try:
        sent = run(conn)
    finally:
        eventlog.unbind_sink()

    assert sent == 2
    assert set(sent_to) == {900901, 900902}  # the two idle users, not the active one

    # §17: the suppressed (active) user is a deliberate skip, not a failure — it
    # counts as `skipped`, never `failed`. One `ok` event carries the run's shape.
    assert len(events) == 1
    assert events[0]["event"] == "job.noon"
    assert events[0]["status"] == "ok"
    assert (events[0]["considered"], events[0]["delivered"], events[0]["skipped"], events[0]["failed"]) == (3, 2, 1, 0)
    conn.rollback()


def test_run_reads_last_evening_from_reminder_log(conn, monkeypatch):
    """Suppression keys on the *actual* last evening summary, not the nominal 21:00.

    The user's last evening summary went out 10 days ago (a long gap), and they
    logged a transaction 5 days ago. Reading `reminder_log`, that activity is
    *after* the boundary, so the nudge is suppressed. If the boundary fell back to
    the nominal previous 21:00 (within the last day), the 5-day-old row would sit
    *before* it and the user would be wrongly nudged — this is the flip the guard
    pins.
    """
    migrate(conn)
    boundary = previous_evening_ist()  # nominal fallback: within the last day
    user = get_or_create_user(conn, 910910)

    _log_evening(conn, user, boundary - timedelta(days=10))  # real last summary, long ago
    _insert(conn, user, boundary - timedelta(days=5))  # after it, before the nominal 21:00

    sent_to = []
    monkeypatch.setattr(noon, "send_message", lambda tg_id, text: sent_to.append(tg_id))

    sent = run(conn)

    assert sent == 0
    assert sent_to == []  # suppressed: active since the real last summary
    conn.rollback()


def test_noon_suppression_is_per_person_not_household_wide(conn, monkeypatch):
    """A housemate's activity must not suppress an idle member's nudge (§12, §16).

    Two members share one household. `active` logs a transaction after the
    boundary; `idle` logs nothing. §16 makes suppression *per person*, so `idle`
    must still be nudged even though a housemate was active. This is the one rule a
    household-wide `logged_since` (scoping only on `household_id`, dropping the
    `user_id` predicate) silently breaks: it would see the household's activity and
    suppress `idle` too. The check reddens if that regression lands.
    """
    migrate(conn)
    boundary = previous_evening_ist()

    active = get_or_create_user(conn, 930930)
    idle = get_or_create_user(conn, 930931)
    join_household(conn, household_of(conn, active), idle)  # same household
    _insert(conn, active, boundary + timedelta(minutes=1))  # housemate is active

    sent_to = []
    monkeypatch.setattr(noon, "send_message", lambda tg_id, text: sent_to.append(tg_id))
    sent = run(conn)

    assert sent == 1
    assert sent_to == [930931]  # idle member nudged; active housemate suppressed
    conn.rollback()


def test_run_logs_a_noon_reminder_for_each_nudged_user(conn, monkeypatch):
    """A nudged user gets a `reminder_log` 'noon' row; a suppressed one does not (§12)."""
    migrate(conn)
    boundary = previous_evening_ist()
    idle = get_or_create_user(conn, 920920)
    active = get_or_create_user(conn, 920921)
    _insert(conn, active, boundary + timedelta(minutes=1))

    monkeypatch.setattr(noon, "send_message", lambda tg_id, text: None)
    run(conn)

    assert _reminder_kinds(conn, idle) == ["noon"]
    assert _reminder_kinds(conn, active) == []
    conn.rollback()
