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
from tests.conftest import join_household

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

    assert roster == [(owner, True, None, None), (member, False, "ravi", None)]
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


def test_household_footer_names_the_commands_and_hides_owner_only_ones(conn, monkeypatch):
    """The multi-member reply is the only place `/remove` and `/transfer` are ever
    named, and a member is never shown a command that would refuse them (§16).

    Both described themselves only inside their own error paths, so before this
    footer the two features were reachable only by already knowing they existed —
    and `/invite` was named solely in the *solo* reply, disappearing exactly when a
    household gained the second person who makes it useful.

    Owner-only commands are gated on the viewer, not merely listed: telling a
    member to `/invite` earns them a refusal they did nothing to deserve. The
    member's `/remove` line is the self-removal form, which is genuinely theirs
    (§16 — owner-only removal would trap them in a ledger they cannot leave).
    """
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_labelled_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    handle_household(conn, _msg(OWNER_TG))
    (_, owner_view) = sent[-1]
    assert "/invite" in owner_view
    assert "/remove <name>" in owner_view
    assert "/transfer" in owner_view

    handle_household(conn, _msg(MEMBER_TG))
    (_, member_view) = sent[-1]
    assert "/remove" in member_view and "leave this household" in member_view
    assert "/invite" not in member_view, "a member cannot invite — don't offer it"
    assert "/transfer" not in member_view, "a member cannot transfer — don't offer it"
    conn.rollback()


def test_the_roster_names_the_owner_instead_of_the_bare_word_owner(conn, monkeypatch):
    """The bug this fixes, from a live screenshot: a member ran `/household` and
    read "• Owner" — no name, no way to tell who owned it.

    There was nothing to print. Member names come from the *invite label* the
    owner typed, and the owner joined by creating the household, so they carry no
    label; nothing else about a person was stored at all. Telegram sends
    `from.first_name` on every update, and it was being dropped.

    Also guards the subtler half: a member's own Telegram name beats the label the
    inviter chose for them. `/invite wife` must not show "wife" to the person
    themselves.
    """
    migrate(conn)
    owner = get_or_create_user(conn, OWNER_TG, "Joshy")
    hid = create_household_of_one(conn, owner)
    member = get_or_create_user(conn, MEMBER_TG, "Diana")
    join_household(conn, hid, member)
    with conn.cursor() as cur:  # the inviter labelled her something else
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by,"
            " used_by, used_at) VALUES ('c1', 'household', %s, 'wife', %s, %s, now())",
            (hid, owner, member),
        )

    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text, **kw: sent.append(text))
    handle_household(conn, TextMessage(chat_id=MEMBER_TG, message_id=1,
                                      text="/household", from_id=MEMBER_TG))

    assert "Joshy (owner)" in sent[0]  # the owner has a name at last
    assert "• Owner" not in sent[0]  # and it is not the bare role
    assert "Diana" in sent[0]  # her own name...
    assert "wife" not in sent[0]  # ...not the label chosen for her
    conn.rollback()


def test_a_rename_in_telegram_shows_up_and_costs_nothing_when_unchanged(conn):
    """Kept fresh, not written once — a roster showing a name someone abandoned is
    worse than one showing none. The `IS DISTINCT FROM` makes the unchanged case
    (every message after the first) touch no row, so this is not one write per
    message forever."""
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG, "Joshy")
    assert get_or_create_user(conn, OWNER_TG, "Joshy Sunny") == uid

    with conn.cursor() as cur:
        cur.execute("SELECT display_name FROM users WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] == "Joshy Sunny"
        # Resolving without a name never blanks the one already stored.
        get_or_create_user(conn, OWNER_TG)
        cur.execute("SELECT display_name FROM users WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] == "Joshy Sunny"
    conn.rollback()


def test_dispatch_carries_the_senders_name_off_both_update_shapes():
    """A name that never reaches a handler cannot be stored. Both shapes read it
    off the same `from` object they already read the id from."""
    msg = handlers.dispatch({
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": 7}, "text": "hi",
                    "from": {"id": 7, "first_name": "Diana"}},
    })
    assert msg.display_name == "Diana"
    press = handlers.dispatch({
        "update_id": 2,
        "callback_query": {"id": "cb", "data": "confirm",
                           "from": {"id": 7, "first_name": "Diana"},
                           "message": {"message_id": 2, "chat": {"id": 7}}},
    })
    assert press.display_name == "Diana"
    # An update with no name at all must not explode.
    bare = handlers.dispatch({
        "update_id": 3,
        "message": {"message_id": 3, "chat": {"id": 7}, "text": "hi", "from": {"id": 7}},
    })
    assert bare.display_name is None
