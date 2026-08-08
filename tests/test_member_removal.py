"""Member removal (§16): the owner removes anyone, a member removes themselves.

One code path (`remove_member`) with the authorization baked in, not at the call
site: a non-owner can only remove *themselves*, and the owner cannot leave while
they still own the household (§16 — a household always has an owner; ownership
transfer is a later task). Removal *retains* the departing member's entries in the
shared ledger — this task never deletes a `transactions` row — and re-homes the
removed member into a fresh household of one so their bot keeps working.
"""

from decimal import Decimal

from conftest import household_of

from kanakko import handlers
from kanakko.db import (
    create_household_of_one,
    get_or_create_user,
    household_roster,
    remove_member,
)
from kanakko.handlers import TextMessage, handle_remove
from kanakko.migrate import migrate

OWNER_TG = 111
MEMBER_TG = 222
OTHER_TG = 333


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    return sent


def _msg(from_id, text="/remove"):
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


def _household_of(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT household_id FROM household_members WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


def _insert_txn(conn, user_id, amount):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, note, occurred_on)"
            " VALUES (%s, %s, %s, 'expense', NULL, NULL, '2026-08-08')",
            (user_id, household_of(conn, user_id), amount),
        )


# --- db layer: remove_member -------------------------------------------------

def test_owner_removes_a_member_retaining_their_entries_and_re_homing_them(conn):
    """The owner removes a member; the member's entries stay, the member is re-homed.

    Retain is the point being proved: the departed member's ₹500 must remain in the
    household it was entered against (this task never deletes it — deletion is the
    next task, §16), so the shared-household total is unchanged. And the member must
    land in a new household of one, or their next confirm would hit the NOT NULL
    `transactions.household_id` (migration 008).
    """
    migrate(conn)
    owner, hid = _seed_owner(conn)
    member = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    _insert_txn(conn, member, Decimal("500.00"))

    assert remove_member(conn, owner, member) == "removed"

    # Gone from the shared roster.
    assert household_roster(conn, owner) == [(owner, True, None)]
    # Re-homed into a fresh household of one (not the one they left).
    new_hid = _household_of(conn, member)
    assert new_hid is not None and new_hid != hid
    # Retained: the ₹500 still belongs to the household they left.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE household_id = %s", (hid,))
        assert cur.fetchone()[0] == 1
    conn.rollback()


def test_a_member_removes_themselves(conn):
    """A member leaves (actor == target) — the self case §16 folds into one path."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    member = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")

    assert remove_member(conn, member, member) == "removed"

    assert household_roster(conn, owner) == [(owner, True, None)]
    assert _household_of(conn, member) != hid  # re-homed
    conn.rollback()


def test_a_member_cannot_remove_another_member(conn):
    """Owner-removes-others stays owner-only (§16): a member can only remove itself."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    ravi = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    priya = _seed_member(conn, hid, owner, OTHER_TG, "priya")

    assert remove_member(conn, ravi, priya) == "not_owner"

    # Nobody removed — both still in the household.
    assert {m for m, _o, _l in household_roster(conn, owner)} == {owner, ravi, priya}
    conn.rollback()


def test_the_owner_cannot_leave_without_transferring_ownership(conn):
    """An owner's self-removal is refused (§16) — a household always has an owner."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")

    assert remove_member(conn, owner, owner) == "owner_must_transfer"

    # Still there, still the owner.
    assert _household_of(conn, owner) == hid
    assert household_roster(conn, owner)[0] == (owner, True, None)
    conn.rollback()


def test_removing_someone_outside_the_household_is_refused(conn):
    """A target in a different household is `not_member` — removal never spans households."""
    migrate(conn)
    owner, _hid = _seed_owner(conn)
    stranger = get_or_create_user(conn, OTHER_TG)
    create_household_of_one(conn, stranger)

    assert remove_member(conn, owner, stranger) == "not_member"
    conn.rollback()


# --- handler layer: handle_remove -------------------------------------------

def test_handle_remove_owner_removes_a_member_by_label(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    ravi = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_remove(conn, _msg(OWNER_TG, "/remove ravi")) == ravi
    assert "Removed ravi" in sent[-1][1]
    assert ravi not in {m for m, _o, _l in household_roster(conn, owner)}
    conn.rollback()


def test_handle_remove_bare_leaves_yourself(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    member = _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_remove(conn, _msg(MEMBER_TG)) == member
    assert "left the household" in sent[-1][1]
    assert household_roster(conn, owner) == [(owner, True, None)]
    conn.rollback()


def test_handle_remove_owner_leaving_is_refused(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_remove(conn, _msg(OWNER_TG)) is None
    assert sent[-1] == (OWNER_TG, handlers.REMOVE_OWNER_MUST_TRANSFER)
    assert _household_of(conn, owner) == hid
    conn.rollback()


def test_handle_remove_member_cannot_remove_another_via_label(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    priya = _seed_member(conn, hid, owner, OTHER_TG, "priya")
    sent = _stub_send(monkeypatch)

    # ravi (a member) tries to remove priya by label.
    assert handle_remove(conn, _msg(MEMBER_TG, "/remove priya")) is None
    assert sent[-1] == (MEMBER_TG, handlers.REMOVE_NOT_OWNER)
    assert priya in {m for m, _o, _l in household_roster(conn, owner)}
    conn.rollback()


def test_handle_remove_unknown_label(conn, monkeypatch):
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_remove(conn, _msg(OWNER_TG, "/remove nobody")) is None
    assert "nobody" in sent[-1][1] and "No member" in sent[-1][1]
    conn.rollback()


def test_handle_remove_ambiguous_label_removes_nobody(conn, monkeypatch):
    """Labels aren't unique; two `ravi`s means I won't guess which to remove."""
    migrate(conn)
    owner, hid = _seed_owner(conn)
    _seed_member(conn, hid, owner, MEMBER_TG, "ravi")
    _seed_member(conn, hid, owner, OTHER_TG, "ravi")
    sent = _stub_send(monkeypatch)

    assert handle_remove(conn, _msg(OWNER_TG, "/remove ravi")) is None
    assert "More than one" in sent[-1][1]
    assert len(household_roster(conn, owner)) == 3  # nobody removed
    conn.rollback()


def test_is_remove_recognises_the_command():
    assert handlers._is_remove("/remove")
    assert handlers._is_remove("/remove ravi")
    assert handlers._is_remove("/remove@kanakko_bot")  # group form
    assert not handlers._is_remove("remove")
    assert not handlers._is_remove("/removed")


# --- roster label scope (regression guard for the join fix) -----------------

def test_roster_label_is_scoped_to_the_current_household(conn):
    """A member who left one household and joined another shows only the new label.

    Once removal lets a user leave household A (invite label "old") and join
    household B (invite label "new"), that user carries a used household invite from
    *each*. An unscoped label join (`i.used_by = m.user_id AND kind = 'household'`)
    matches both and duplicates the user's roster row in B with the stale "old"
    label. The join is scoped to `i.household_id = m.household_id`, so B's roster
    shows exactly one row for them, labelled "new".
    """
    migrate(conn)
    owner_a, hid_a = _seed_owner(conn)
    mover = _seed_member(conn, hid_a, owner_a, MEMBER_TG, "old")

    remove_member(conn, owner_a, mover)  # mover leaves A (keeps A's used invite)

    owner_b = get_or_create_user(conn, OTHER_TG)
    hid_b = create_household_of_one(conn, owner_b)
    # mover joins B under a new label; drop their re-homed household-of-one first.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM household_members WHERE user_id = %s", (mover,))
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (hid_b, mover),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by, used_by)"
            " VALUES ('h-b-new', 'household', %s, 'new', %s, %s)",
            (hid_b, owner_b, mover),
        )

    roster = household_roster(conn, mover)
    assert roster == [(owner_b, True, None), (mover, False, "new")]
    conn.rollback()
