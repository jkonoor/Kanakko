"""`/invite`: an owner issues a labelled single-use household link (§16).

The two guards this proves, both from §16: an invite is **owner-only** (a member
who isn't the owner mints no code), and the code it issues is a **household** kind
homed in the owner's own household, labelled so the operator can tell who is
active. A bare `/invite` with no label stores nothing.
"""

from kanakko.commands import invite as invite_command
from kanakko.commands.invite import handle_invite
from kanakko.db import (
    create_household_invite,
    create_household_of_one,
    get_or_create_user,
)
from kanakko.handlers import TextMessage
from kanakko.migrate import migrate

OWNER_TG = 111
MEMBER_TG = 222

BOT = "kanakko_bot"


def _stub_telegram(monkeypatch):
    """No network: capture sends, pin the bot username the link is built from."""
    sent = []
    monkeypatch.setattr(invite_command, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    monkeypatch.setattr(invite_command, "get_bot_username", lambda: BOT)
    return sent


def _invite(text, from_id=OWNER_TG):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _invite_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code, kind, household_id, label, created_by FROM invites"
            " ORDER BY invite_id"
        )
        return cur.fetchall()


def _seed_owner(conn):
    owner = get_or_create_user(conn, OWNER_TG)
    hid = create_household_of_one(conn, owner)
    return owner, hid


def _seed_member(conn, hid):
    """A second user in the owner's household — a member, not the owner."""
    member = get_or_create_user(conn, MEMBER_TG)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (hid, member),
        )
    return member


def test_owner_issues_a_labelled_household_invite(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    sent = _stub_telegram(monkeypatch)

    code = handle_invite(conn, _invite("/invite ravi"))

    rows = _invite_rows(conn)
    assert len(rows) == 1
    stored_code, kind, household_id, label, created_by = rows[0]
    assert stored_code == code  # the returned code is the one persisted
    assert kind == "household"  # a household invite, not a signup (§16)
    assert household_id == hid  # homed in the owner's own household
    assert label == "ravi"  # labelled so the operator can attribute it (§16)
    assert created_by == owner
    # The reply is the deep link the invitee taps, carrying the exact code.
    (chat_id, text) = sent[0]
    assert chat_id == OWNER_TG
    assert f"https://t.me/{BOT}?start={code}" in text
    conn.rollback()


def test_member_who_isnt_owner_is_refused(conn, monkeypatch):
    """Owner-only (§16): a non-owner member mints no code — the guard that reddens
    if `create_household_invite`'s `WHERE owner = %s` scope is dropped."""
    migrate(conn)
    _owner, hid = _seed_owner(conn)
    _seed_member(conn, hid)
    sent = _stub_telegram(monkeypatch)

    code = handle_invite(conn, _invite("/invite priya", from_id=MEMBER_TG))

    assert code is None
    assert _invite_rows(conn) == []  # nothing issued
    assert sent == [(MEMBER_TG, invite_command.INVITE_NOT_OWNER)]
    conn.rollback()


def test_bare_invite_asks_for_a_label(conn, monkeypatch):
    migrate(conn)
    _seed_owner(conn)
    sent = _stub_telegram(monkeypatch)

    code = handle_invite(conn, _invite("/invite"))

    assert code is None
    assert _invite_rows(conn) == []  # no label, no code
    assert sent == [(OWNER_TG, invite_command.INVITE_USAGE)]
    conn.rollback()


def test_create_household_invite_scopes_to_the_owner(conn):
    """The SQL guard in isolation: a non-owner user id inserts nothing, so the
    end-to-end refusal above can't be an artefact of the handler alone."""
    migrate(conn)
    _owner, hid = _seed_owner(conn)
    member = _seed_member(conn, hid)

    assert create_household_invite(conn, member, "h-nope", "x") is False
    assert _invite_rows(conn) == []
    assert create_household_invite(conn, _owner_id(conn), "h-yes", "y") is True
    conn.rollback()


def _owner_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE telegram_user_id = %s", (OWNER_TG,))
        (uid,) = cur.fetchone()
    return uid


def test_is_invite_recognises_the_command():
    assert invite_command._is_invite("/invite ravi")
    assert invite_command._is_invite("/invite@kanakko_bot ravi")  # group form
    assert not invite_command._is_invite("invite ravi")
    assert not invite_command._is_invite("/invitation ravi")
