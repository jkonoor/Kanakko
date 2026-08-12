"""The bot-facing refund UX (§18, split 2/2): `refund <amount>` lists live,
not-fully-refunded expenses to pick from, one tap calls `db.refunds.create_refund`.

`256ba0b`/`3bfbf58` landed the money-correctness half — schema, the guard trigger,
report netting. This is pure UX wiring on top of it: `refund_candidates` is the
query the chooser lists from, `handle_refund`/`handle_refund_choice` are the two
handler halves (list, then act on a tap), and the property this suite turns on is
that an over-limit tap is caught and answered, never a 500.
"""

from datetime import date
from decimal import Decimal

from conftest import default_account_of, household_of, join_household

from kanakko import handlers
from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.db import create_refund, get_or_create_user, refund_candidates
from kanakko.handlers import (
    REFUND_PREFIX,
    ButtonPress,
    TextMessage,
    handle_refund,
    handle_refund_choice,
)
from kanakko.migrate import migrate

OWNER_TG = 111
MEMBER_TG = 222
OTHER_TG = 333


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text, reply_markup=None: sent.append((chat_id, text, reply_markup)))
    return sent


def _stub_taps(monkeypatch):
    edits, acks = [], []
    monkeypatch.setattr(handlers, "edit_message_text",
                        lambda chat_id, message_id, text, reply_markup=None: edits.append(text))
    monkeypatch.setattr(handlers, "answer_callback_query",
                        lambda cbq, text=None: acks.append(text))
    return edits, acks


def _msg(from_id, text="refund"):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _tap(from_id, txn_id, amount):
    return ButtonPress(chat_id=from_id, message_id=9, callback_query_id="cbq",
                       data=f"{REFUND_PREFIX}{txn_id}:{amount}",
                       from_id=from_id, update_id=2)


def _insert_expense(conn, user_id, amount, category=None, day="2026-08-10"):
    hh = household_of(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, occurred_on, account_id)"
            " VALUES (%s, %s, %s, 'expense', %s, %s, %s) RETURNING txn_id",
            (user_id, hh, amount, category or EXPENSE_CATEGORIES[0], day,
             default_account_of(conn, hh)),
        )
        (txn_id,) = cur.fetchone()
    return txn_id


# --- db layer: refund_candidates ---------------------------------------------

def test_refund_candidates_puts_the_amount_match_first(conn):
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    older_match = _insert_expense(conn, uid, Decimal("500.00"), day="2026-08-01")
    _insert_expense(conn, uid, Decimal("300.00"), day="2026-08-05")

    candidates = refund_candidates(conn, uid, Decimal("500.00"))

    assert candidates[0]["txn_id"] == older_match  # exact-amount match wins over recency
    assert {c["txn_id"] for c in candidates} == {older_match, candidates[1]["txn_id"]}
    conn.rollback()


def test_refund_candidates_excludes_a_fully_refunded_expense(conn):
    """A ₹500 expense refunded to ₹0 must not reappear as a candidate."""
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    txn_id = _insert_expense(conn, uid, Decimal("500.00"))

    create_refund(conn, uid, txn_id, Decimal("500.00"), date(2026, 8, 11),
                  source="webhook", update_id=None)

    assert refund_candidates(conn, uid, Decimal("500.00")) == []
    conn.rollback()


def test_refund_candidates_includes_a_partially_refunded_expense(conn):
    """₹200 left on a ₹500 expense after a ₹300 refund is still a live candidate."""
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    txn_id = _insert_expense(conn, uid, Decimal("500.00"))

    create_refund(conn, uid, txn_id, Decimal("300.00"), date(2026, 8, 11),
                  source="webhook", update_id=None)

    candidates = refund_candidates(conn, uid, Decimal("200.00"))
    assert [c["txn_id"] for c in candidates] == [txn_id]
    assert candidates[0]["remaining"] == Decimal("200.00")
    conn.rollback()


def test_refund_candidates_is_household_scoped(conn):
    """§16: any member's expense is a candidate; a stranger's household is invisible."""
    migrate(conn)
    owner = get_or_create_user(conn, OWNER_TG)
    hid = household_of(conn, owner)
    member = get_or_create_user(conn, MEMBER_TG)
    join_household(conn, hid, member)
    stranger = get_or_create_user(conn, OTHER_TG)

    member_txn = _insert_expense(conn, member, Decimal("500.00"))
    _insert_expense(conn, stranger, Decimal("500.00"))

    candidates = refund_candidates(conn, owner, Decimal("500.00"))
    assert [c["txn_id"] for c in candidates] == [member_txn]
    conn.rollback()


def test_refund_candidates_never_lists_income_or_transfers(conn):
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    hh = household_of(conn, uid)
    acc = default_account_of(conn, hh)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, occurred_on, account_id)"
            " VALUES (%s, %s, 500.00, 'income', 'Salary', '2026-08-10', %s)",
            (uid, hh, acc),
        )

    assert refund_candidates(conn, uid, Decimal("500.00")) == []
    conn.rollback()


# --- handler layer: handle_refund --------------------------------------------

def test_handle_refund_lists_candidates_with_amount_in_callback_data(conn, monkeypatch):
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    txn_id = _insert_expense(conn, uid, Decimal("500.00"))
    sent = _stub_send(monkeypatch)

    assert handle_refund(conn, _msg(OWNER_TG, "refund 500")) == [{
        "txn_id": txn_id, "amount": Decimal("500.00"),
        "category": EXPENSE_CATEGORIES[0], "occurred_on": date(2026, 8, 10),
        "remaining": Decimal("500.00"),
    }]
    _chat, _text, keyboard = sent[-1]
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert datas == [f"{REFUND_PREFIX}{txn_id}:500.00"]
    conn.rollback()


def test_handle_refund_bare_is_a_usage_hint(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    sent = _stub_send(monkeypatch)

    assert handle_refund(conn, _msg(OWNER_TG, "refund")) is None
    assert sent[-1][:2] == (OWNER_TG, handlers.REFUND_USAGE)
    conn.rollback()


def test_handle_refund_bad_amount(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    sent = _stub_send(monkeypatch)

    assert handle_refund(conn, _msg(OWNER_TG, "refund lots")) is None
    assert sent[-1][:2] == (OWNER_TG, handlers.REFUND_BAD_AMOUNT)
    conn.rollback()


def test_handle_refund_no_candidates(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    sent = _stub_send(monkeypatch)

    assert handle_refund(conn, _msg(OWNER_TG, "refund 500")) is None
    assert sent[-1][:2] == (OWNER_TG, handlers.REFUND_NO_CANDIDATES)
    conn.rollback()


# --- handler layer: handle_refund_choice -------------------------------------

def test_handle_refund_choice_creates_the_refund(conn, monkeypatch):
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    txn_id = _insert_expense(conn, uid, Decimal("500.00"))
    edits, acks = _stub_taps(monkeypatch)

    refunded = handle_refund_choice(conn, _tap(OWNER_TG, txn_id, "500.00"))

    assert refunded["txn_id"] != txn_id  # a new row, not the original
    assert refunded["amount"] == Decimal("500.00")
    assert acks[-1] == "Refunded"
    assert "Refunded" in edits[-1]
    candidates = refund_candidates(conn, uid, Decimal("1.00"))
    assert candidates == []  # fully refunded now — nothing left to take
    conn.rollback()


def test_handle_refund_choice_over_limit_is_caught_not_500(conn, monkeypatch):
    """The trigger's `RaiseException` on an over-limit tap must not propagate — the
    exact regression this handler exists to close: two members racing the same
    chooser, or a tap on a candidate the amount doesn't actually fit."""
    migrate(conn)
    uid = get_or_create_user(conn, OWNER_TG)
    txn_id = _insert_expense(conn, uid, Decimal("500.00"))
    edits, acks = _stub_taps(monkeypatch)

    # The chooser was built for 500, but the button also carries an amount that
    # would overshoot a smaller original — simulate a stale/forged tap for 600.
    result = handle_refund_choice(conn, _tap(OWNER_TG, txn_id, "600.00"))

    assert result is None
    assert acks[-1] == handlers.REFUND_OVER_LIMIT
    assert edits == []  # no card was edited — nothing was stored
    # The connection must still be usable — a poisoned transaction would raise here.
    assert refund_candidates(conn, uid, Decimal("500.00"))[0]["remaining"] == Decimal("500.00")
    conn.rollback()


def test_handle_refund_choice_gone_expense(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    edits, acks = _stub_taps(monkeypatch)

    assert handle_refund_choice(conn, _tap(OWNER_TG, 999999, "500.00")) is None
    assert acks[-1] == handlers.REFUND_GONE
    conn.rollback()


def test_handle_refund_choice_reauthorizes_a_forged_household(conn, monkeypatch):
    """§16: `create_refund` re-scopes to the presser's own household — a forged tap
    naming another household's `txn_id` finds no matching live row."""
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    stranger = get_or_create_user(conn, OTHER_TG)
    stranger_txn = _insert_expense(conn, stranger, Decimal("500.00"))
    edits, acks = _stub_taps(monkeypatch)

    assert handle_refund_choice(conn, _tap(OWNER_TG, stranger_txn, "500.00")) is None
    assert acks[-1] == handlers.REFUND_GONE
    conn.rollback()


def test_handle_refund_choice_malformed_button(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, OWNER_TG)
    edits, acks = _stub_taps(monkeypatch)
    press = ButtonPress(chat_id=OWNER_TG, message_id=9, callback_query_id="cbq",
                        data=f"{REFUND_PREFIX}notanumber:500.00",
                        from_id=OWNER_TG, update_id=2)

    assert handle_refund_choice(conn, press) is None
    assert acks[-1] == "That button's expired"
    conn.rollback()


# --- predicate ----------------------------------------------------------------

def test_is_refund_recognises_the_bare_word():
    assert handlers._is_refund("refund 500")
    assert handlers._is_refund("REFUND 500")
    assert handlers._is_refund("refund")
    assert not handlers._is_refund("refunded 500")
    assert not handlers._is_refund("I want a refund")
    assert not handlers._is_refund("")
