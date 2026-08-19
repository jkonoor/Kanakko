"""`/delete_account`: the way out of the product, and the dead end it closes (§16).

Two gaps, one fix. A solo owner could not leave — they own the household, an
owner must transfer first, and there is nobody to transfer to — and *nobody*
could erase their data at all, because leaving only ever re-homed you into a new
household of one.

The guards worth having here are the ones that would let erasure take too much
or too little: an owner must not be able to erase themselves out from under
their household's other members; a member leaving a shared household must not
take the household with them; and the scrubbed row must stop behaving like a
live user, or the evening job posts a summary to a negative chat id.
"""

from datetime import date
from decimal import Decimal

import pytest

from kanakko.commands import erase as erase_command
from kanakko.commands.erase import handle_erase, handle_erase_choice
from kanakko.db import (
    all_users,
    check_removal,
    create_household_of_one,
    delete_account,
    find_user,
    get_or_create_user,
)
from kanakko.handlers import ButtonPress, TextMessage
from kanakko.migrate import migrate
from tests.conftest import join_household

OWNER_TG = 9001
MEMBER_TG = 9002


def _stub(monkeypatch):
    sent = []
    monkeypatch.setattr(erase_command, "send_message",
                        lambda chat_id, text, **kw: sent.append(text))
    monkeypatch.setattr(erase_command, "answer_callback_query", lambda *a, **k: None)
    monkeypatch.setattr(erase_command, "edit_message_text",
                        lambda chat_id, mid, text, **kw: sent.append(text))
    return sent


def _msg(text, from_id=OWNER_TG):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _press(from_id=OWNER_TG):
    return ButtonPress(chat_id=from_id, message_id=2, callback_query_id="cb",
                       data="erase:yes", from_id=from_id, update_id=2)


def _seed(conn, tg):
    uid = get_or_create_user(conn, tg)
    hid = create_household_of_one(conn, uid)
    with conn.cursor() as cur:
        # `create_household_of_one` already mints the default account (Phase 11),
        # so this reuses it rather than tripping the one-default-per-household index.
        cur.execute(
            "SELECT account_id FROM accounts WHERE household_id = %s AND is_default",
            (hid,))
        (acct,) = cur.fetchone()
        cur.execute(
            "INSERT INTO transactions (user_id, household_id, account_id, amount,"
            " type, category, occurred_on) VALUES (%s,%s,%s,%s,'expense','Food',%s)",
            (uid, hid, acct, Decimal("500.00"), date(2026, 8, 1)))
    return uid, hid, acct


def test_a_solo_owner_is_told_the_truth_instead_of_the_impossible(conn):
    """The dead end: `/remove` told the only member of a household to hand
    ownership to someone first. There is nobody to hand it to. The refusal now
    names its own case, which is what lets the reply point at the real exit."""
    migrate(conn)
    uid, _hid, _acct = _seed(conn, OWNER_TG)

    assert check_removal(conn, uid, uid) == "owner_alone"
    assert "/delete_account" in erase_command.ERASE_COMMAND
    conn.rollback()


def test_an_owner_with_members_still_cannot_erase_themselves(conn):
    """The rule §16 already applies to leaving, applied to erasure: a household
    always has an owner, and taking one out from under its members is not the
    leaver's call. Reddens if erasure ever skips the ownership check."""
    migrate(conn)
    owner, hid, _ = _seed(conn, OWNER_TG)
    join_household(conn, hid, get_or_create_user(conn, MEMBER_TG))

    assert delete_account(conn, owner) == "owner_must_transfer"
    assert find_user(conn, OWNER_TG) == owner  # still very much here
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transactions WHERE user_id = %s", (owner,))
        assert cur.fetchone()[0] == 1  # and nothing of theirs was touched
    conn.rollback()


def test_erasing_a_solo_owner_takes_the_household_with_them(conn):
    migrate(conn)
    uid, hid, _acct = _seed(conn, OWNER_TG)

    assert delete_account(conn, uid) == "ok"

    with conn.cursor() as cur:
        for table, column in (("transactions", "user_id"), ("household_members", "user_id")):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s", (uid,))
            assert cur.fetchone()[0] == 0, f"{table} survived"
        for table in ("accounts", "households"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE household_id = %s", (hid,))
            assert cur.fetchone()[0] == 0, f"{table} survived"
    conn.rollback()


def test_a_member_leaving_a_shared_household_does_not_take_it_with_them(conn):
    """Erasure must delete exactly one person's data. A member who erases takes
    their own entries and nothing else — the household, its owner and their
    entries all stay."""
    migrate(conn)
    owner, hid, _ = _seed(conn, OWNER_TG)
    member = get_or_create_user(conn, MEMBER_TG)
    join_household(conn, hid, member)

    assert delete_account(conn, member) == "ok"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM households WHERE household_id = %s", (hid,))
        assert cur.fetchone()[0] == 1  # the household lives on
        cur.execute("SELECT count(*) FROM transactions WHERE user_id = %s", (owner,))
        assert cur.fetchone()[0] == 1  # the owner's entry is untouched
        cur.execute("SELECT owner FROM accounts WHERE household_id = %s", (hid,))
        assert cur.fetchone()[0] == owner  # nothing points at the scrubbed row
    conn.rollback()


def test_a_scrubbed_row_stops_looking_like_a_live_user(conn):
    """The `users` row is kept so eleven foreign keys stay valid — including
    `invites.created_by`, which belongs to other people's history. What must not
    survive is the *identity*: a lookup can never find them again, and the jobs'
    fan-out must not send an evening summary to chat id -42."""
    migrate(conn)
    uid, _hid, _ = _seed(conn, OWNER_TG)

    delete_account(conn, uid)

    assert find_user(conn, OWNER_TG) is None
    assert uid not in [u for u, _tg in all_users(conn)]
    with conn.cursor() as cur:
        cur.execute("SELECT telegram_user_id FROM users WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] == -uid  # negative: never a real Telegram id
    # The real id is free again — a later invite starts genuinely fresh.
    assert get_or_create_user(conn, OWNER_TG) != uid
    conn.rollback()


def test_the_typed_command_only_asks_and_the_tap_is_what_acts(conn, monkeypatch):
    """The destructive step is never the typed command. Reddens if
    `handle_erase` is ever wired straight to `delete_account`."""
    migrate(conn)
    uid, _hid, _ = _seed(conn, OWNER_TG)
    sent = _stub(monkeypatch)

    assert handle_erase(conn, _msg("/delete_account")) is True
    assert find_user(conn, OWNER_TG) == uid  # asked, not acted
    assert "cannot be undone" in sent[0]

    assert handle_erase_choice(conn, _press()) == "ok"
    assert find_user(conn, OWNER_TG) is None
    conn.rollback()


def test_a_second_tap_on_the_same_card_is_expired_not_a_crash(conn, monkeypatch):
    migrate(conn)
    _seed(conn, OWNER_TG)
    _stub(monkeypatch)

    assert handle_erase_choice(conn, _press()) == "ok"
    assert handle_erase_choice(conn, _press()) is None  # the row is already gone
    conn.rollback()


@pytest.mark.parametrize("word", ["/delete_account", "/delete_account@kanakko_bot"])
def test_is_erase_recognises_the_command(word):
    assert erase_command._is_erase(word)


def test_is_erase_does_not_match_its_neighbours():
    assert not erase_command._is_erase("/delete")
    assert not erase_command._is_erase("/remove")
