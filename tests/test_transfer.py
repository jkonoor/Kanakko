"""Ownership transfer (§16): a household always has an owner, so an owner can't
leave until they hand ownership to another member first.

`transfer_ownership` holds the authorization (owner-only, target must be a member
of the same household); `handle_transfer` resolves a `/transfer <label>` against
the sender's roster. The property the task turns on: an owner's self-removal is
refused while they own the household, and succeeds once ownership has moved.
"""

from conftest import household_of

from kanakko.commands import transfer as transfer_module
from kanakko.commands.transfer import handle_transfer
from kanakko.db import (
    create_household_of_one,
    get_or_create_user,
    remove_member,
    transfer_ownership,
)
from kanakko.handlers import TextMessage
from kanakko.migrate import migrate

OWNER_TG = 111
MEMBER_TG = 222
OTHER_TG = 333


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(transfer_module, "send_message",
                        lambda chat_id, text, reply_markup=None: sent.append((chat_id, text, reply_markup)))
    return sent


def _msg(from_id, text="/transfer"):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _seed_owner(conn):
    owner = get_or_create_user(conn, OWNER_TG)
    hid = create_household_of_one(conn, owner)
    return owner, hid


def _seed_member(conn, hid, owner, tg, label):
    """A user who joined `hid` through a labelled household invite (used_by)."""
    member = get_or_create_user(conn, tg)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (hid, member),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by, used_by)"
            " VALUES (%s, 'household', %s, %s, %s, %s)",
            (f"h-{tg}-{label}", hid, label, owner, member),
        )
    return member


def _owner_of(conn, hid):
    with conn.cursor() as cur:
        cur.execute("SELECT owner FROM households WHERE household_id = %s", (hid,))
        return cur.fetchone()[0]


# --- db layer: transfer_ownership -------------------------------------------

def test_owner_transfers_then_can_leave(conn):
    """The task's property: self-removal is refused while owner, allowed after transfer."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    ravi = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")

    # Owner cannot leave while they own it.
    assert remove_member(conn, owner, owner) == "owner_must_transfer"

    assert transfer_ownership(conn, owner, ravi) == "transferred"
    assert _owner_of(conn, hid) == ravi  # ownership actually moved

    # The former owner is now an ordinary member and may leave.
    assert remove_member(conn, owner, owner) == "removed"
    assert household_of(conn, ravi) == hid  # ravi still owns the household
    assert household_of(conn, owner) != hid  # re-homed out
    conn.rollback()


def test_only_owner_can_transfer(conn):
    """A non-owner member cannot hand out ownership (§16)."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    ravi = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    priya = _seed_member(conn, hid, owner, OTHER_TG, "priya")

    assert transfer_ownership(conn, ravi, priya) == "not_owner"
    assert _owner_of(conn, hid) == owner  # unchanged
    conn.rollback()


def test_transfer_to_someone_outside_the_household_is_refused(conn):
    """A target in a different household is `not_member` — transfer never spans households."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    stranger = get_or_create_user(conn, OTHER_TG)
    create_household_of_one(conn, stranger)

    assert transfer_ownership(conn, owner, stranger) == "not_member"
    assert _owner_of(conn, hid) == owner
    conn.rollback()


def test_transfer_to_self_is_already_owner(conn):
    """The defensive path: handing ownership to yourself changes nothing."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")

    assert transfer_ownership(conn, owner, owner) == "already_owner"
    assert _owner_of(conn, hid) == owner
    conn.rollback()


# --- handler layer: handle_transfer -----------------------------------------

def test_handle_transfer_hands_ownership_by_label(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    ravi = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_transfer(conn, _msg(OWNER_TG, "/transfer ravi")) == ravi
    assert _owner_of(conn, hid) == ravi
    assert "ravi" in sent[-1][1]
    conn.rollback()


def test_handle_transfer_bare_is_a_usage_hint(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_transfer(conn, _msg(OWNER_TG)) is None
    assert sent[-1][:2] == (OWNER_TG, transfer_module.TRANSFER_USAGE)
    assert _owner_of(conn, hid) == owner  # nothing moved
    conn.rollback()


def test_handle_transfer_member_cannot(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    _seed_member(conn, hid, owner, OTHER_TG, "priya")
    sent = _stub_send(monkeypatch)

    # ravi (a member) tries to hand priya ownership — refused, nothing moves.
    assert handle_transfer(conn, _msg(MEMBER_TG, "/transfer priya")) is None
    assert sent[-1][:2] == (MEMBER_TG, transfer_module.TRANSFER_NOT_OWNER)
    assert _owner_of(conn, hid) == owner
    conn.rollback()


def test_handle_transfer_unknown_label(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_transfer(conn, _msg(OWNER_TG, "/transfer nobody")) is None
    assert "nobody" in sent[-1][1] and "No member" in sent[-1][1]
    assert _owner_of(conn, hid) == owner
    conn.rollback()


def test_is_transfer_recognises_the_command():
    assert transfer_module._is_transfer("/transfer")
    assert transfer_module._is_transfer("/transfer ravi")
    assert transfer_module._is_transfer("/transfer@kanakko_bot")  # group form
    assert not transfer_module._is_transfer("transfer")
    assert not transfer_module._is_transfer("/transferred")
