"""`/invite_signup`: the operator admits a tester with their own household (§16).

Three guards, each for a way this could quietly become the wrong grant. It is
**operator-only** — gated on `$ADMIN_TELEGRAM_IDS`, not on owning a household, so
an admitted tester cannot admit more people. It issues the **signup** kind with a
NULL household, not the `household` kind `/invite` issues — the difference between
"here is your own ledger" and "here is a seat at mine". And the link it prints
really does end in a household of one when consumed, which is the only assertion
that covers both halves at once.
"""

import pytest

from kanakko import handlers
from kanakko.db import consume_invite, create_household_of_one, get_or_create_user
from kanakko.handlers import TextMessage, handle_invite_signup, handle_start
from kanakko.migrate import migrate

ADMIN_TG = 555
TESTER_TG = 666
NOT_ADMIN_TG = 777

BOT = "kanakko_bot"


@pytest.fixture
def admin_env(monkeypatch):
    """`$ADMIN_TELEGRAM_IDS` names the admin — the env is the whole permission."""
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", f"{ADMIN_TG}")


def _stub_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    monkeypatch.setattr(handlers, "get_bot_username", lambda: BOT)
    return sent


def _msg(text, from_id=ADMIN_TG, update_id=1):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=update_id)


def _invite_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code, kind, household_id, label, created_by FROM invites"
            " ORDER BY invite_id"
        )
        return cur.fetchall()


def test_admin_issues_a_signup_invite(conn, monkeypatch, admin_env):
    migrate(conn)
    admin = get_or_create_user(conn, ADMIN_TG)
    create_household_of_one(conn, admin)
    sent = _stub_telegram(monkeypatch)

    code = handle_invite_signup(conn, _msg("/invite_signup ravi"))

    rows = _invite_rows(conn)
    assert len(rows) == 1
    stored_code, kind, household_id, label, created_by = rows[0]
    assert stored_code == code
    assert kind == "signup"  # the grant that admits, not the one that joins (§16)
    assert household_id is None  # a signup invite homes nobody (the CHECK's half)
    assert label == "ravi"
    assert created_by == admin
    (chat_id, text) = sent[0]
    assert chat_id == ADMIN_TG
    assert f"https://t.me/{BOT}?start={code}" in text
    conn.rollback()


def test_a_non_admin_household_owner_is_refused(conn, monkeypatch, admin_env):
    """The guard that reddens if the gate is ever loosened to "owns a household":
    this user owns one, and still mints nothing."""
    migrate(conn)
    create_household_of_one(conn, get_or_create_user(conn, NOT_ADMIN_TG))
    sent = _stub_telegram(monkeypatch)

    code = handle_invite_signup(conn, _msg("/invite_signup priya", from_id=NOT_ADMIN_TG))

    assert code is None
    assert _invite_rows(conn) == []
    assert sent == [(NOT_ADMIN_TG, handlers.INVITE_SIGNUP_NOT_ADMIN)]
    conn.rollback()


def test_unset_admin_env_admits_nobody(conn, monkeypatch):
    """Fails closed like `SIGNUP_MODE`: no env, no operator, not even the first user."""
    monkeypatch.delenv("ADMIN_TELEGRAM_IDS", raising=False)
    migrate(conn)
    create_household_of_one(conn, get_or_create_user(conn, ADMIN_TG))
    sent = _stub_telegram(monkeypatch)

    assert handle_invite_signup(conn, _msg("/invite_signup ravi")) is None
    assert _invite_rows(conn) == []
    assert sent == [(ADMIN_TG, handlers.INVITE_SIGNUP_NOT_ADMIN)]
    conn.rollback()


def test_bare_command_asks_for_a_label(conn, monkeypatch, admin_env):
    migrate(conn)
    create_household_of_one(conn, get_or_create_user(conn, ADMIN_TG))
    sent = _stub_telegram(monkeypatch)

    assert handle_invite_signup(conn, _msg("/invite_signup")) is None
    assert _invite_rows(conn) == []
    assert sent == [(ADMIN_TG, handlers.INVITE_SIGNUP_USAGE)]
    conn.rollback()


def test_the_issued_link_lands_the_tester_in_their_own_household(
    conn, monkeypatch, admin_env
):
    """End to end: issue, consume via `/start <code>`, and the tester is alone in a
    household that is not the admin's — the whole point of the signup kind."""
    migrate(conn)
    admin = get_or_create_user(conn, ADMIN_TG)
    admin_hid = create_household_of_one(conn, admin)
    _stub_telegram(monkeypatch)

    code = handle_invite_signup(conn, _msg("/invite_signup ravi"))
    handle_start(conn, _msg(f"/start {code}", from_id=TESTER_TG, update_id=2))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT hm.household_id FROM household_members hm"
            " JOIN users u USING (user_id) WHERE u.telegram_user_id = %s",
            (TESTER_TG,),
        )
        (tester_hid,) = cur.fetchone()
        assert tester_hid != admin_hid  # their own household, not a seat in mine
        cur.execute(
            "SELECT count(*) FROM household_members WHERE household_id = %s",
            (tester_hid,),
        )
        assert cur.fetchone()[0] == 1
    # Single-use: the same code cannot admit a second person.
    assert consume_invite(conn, code, 888) == "spent"
    conn.rollback()


def test_is_invite_signup_recognises_the_command():
    assert handlers._is_invite_signup("/invite_signup ravi")
    assert handlers._is_invite_signup("/invite_signup@kanakko_bot ravi")
    assert not handlers._is_invite_signup("/invite ravi")  # never the sibling command
    assert not handlers._is_invite("/invite_signup ravi")  # nor the reverse


def test_admin_ids_parses_a_list_and_survives_junk(monkeypatch):
    """A fat-fingered env must refuse, never crash the webhook (§16 config style)."""
    from kanakko.auth import is_admin

    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", " 111 ,oops, 222,")
    assert is_admin(111) and is_admin(222)
    assert not is_admin(333)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "")
    assert not is_admin(111)
