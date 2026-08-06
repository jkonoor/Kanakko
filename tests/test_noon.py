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
            " (user_id, amount, type, category, note, occurred_on, created_at, deleted_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, Decimal("10.00"), "expense", None, None, date(2026, 8, 6), created_at, deleted_at),
        )


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
    idle_never = get_or_create_user(conn, 900902)  # never logged anything

    _insert(conn, active, boundary + timedelta(minutes=1))
    _insert(conn, idle_stale, boundary - timedelta(hours=1))

    sent_to = []
    monkeypatch.setattr(noon, "send_message", lambda tg_id, text: sent_to.append(tg_id))

    sent = run(conn)

    assert sent == 2
    assert set(sent_to) == {900901, 900902}  # the two idle users, not the active one
    conn.rollback()
