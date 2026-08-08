"""`/household`: who is in it, who owns it (§16).

Two things this proves: `household_roster` lists exactly the querying user's
household — owner first, each member carrying the invite label §16 keeps for
attribution — and `handle_household` marks the owner and the viewer without ever
leaking another household's members. A solo user is pointed at `/invite` rather
than shown a bare list of one.
"""

from kanakko import handlers
from kanakko.db import (
    create_household_of_one,
    get_or_create_user,
    household_roster,
)
from kanakko.handlers import TextMessage, handle_household
from kanakko.migrate import migrate

OWNER_TG = 111
MEMBER_TG = 222
OTHER_TG = 333  # a second household — must never appear in the first's roster


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    return sent


def _msg(from_id, text="/household"):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _seed_owner(conn):
    owner = get_or_create_user(conn, OWNER_TG)
    hid = create_household_of_one(conn, owner)
    return owner, hid


def _seed_labelled_member(conn, hid, owner, tg, label):
    """A member who joined `hid` through a labelled household invite (used_by)."""
    member = get_or_create_user(conn, tg)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (hid, member),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by, used_by)"
            " VALUES (%s, 'household', %s, %s, %s, %s)",
            (f"h-{label}", hid, label, owner, member),
        )
    return member


def test_roster_lists_the_household_owner_first_with_member_labels(conn):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    member = _seed_labelled_member(conn, hid, owner, MEMBER_TG, "ravi")
    # A second, unrelated household — its member must not leak into the first.
    other, other_hid = get_or_create_user(conn, OTHER_TG), None
    other_hid = create_household_of_one(conn, other)
    _seed_labelled_member(conn, other_hid, other, 444, "priya")

    roster = household_roster(conn, owner)

    assert roster == [(owner, True, None), (member, False, "ravi")]
    # Queried from the member: same household, same rows — scope is the household,
    # not the caller.
    assert household_roster(conn, member) == roster
    conn.rollback()


def test_handle_household_marks_the_owner_and_the_viewer(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_labelled_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_household(conn, _msg(OWNER_TG)) == "ok"
    (_, text) = sent[-1]
    assert "2 members" in text
    assert "Owner (you)" in text  # the owner is viewing
    assert "• ravi" in text and "ravi (you)" not in text

    handle_household(conn, _msg(MEMBER_TG))
    (_, text) = sent[-1]
    assert "Owner" in text and "Owner (you)" not in text  # member is viewing
    assert "ravi (you)" in text
    conn.rollback()


def test_solo_household_points_at_invite(conn, monkeypatch):
    migrate(conn)
    _seed_owner(conn)
    sent = _stub_send(monkeypatch)

    handle_household(conn, _msg(OWNER_TG))

    assert sent == [(OWNER_TG, handlers.HOUSEHOLD_SOLO)]
    conn.rollback()


def test_is_household_recognises_the_command():
    assert handlers._is_household("/household")
    assert handlers._is_household("/household@kanakko_bot")  # group form
    assert not handlers._is_household("household")
    assert not handlers._is_household("/households")
