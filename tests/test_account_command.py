"""`/account`: the onboarding ask for a credit card or locked savings (§18).

`WELCOME` points a new user at this command rather than an interactive wizard —
everyday spending already has a default account (minted by
`create_household_of_one`), so the command only ever adds the other two kinds.
The guard this proves: a bad kind or a bad amount refuses and stores nothing, a
good call sets the signed opening balance (credit negated — "how much you owe"
reads back as what is owed, never what is in the account), and a repeat call
corrects the same account rather than minting a second one.
"""

from decimal import Decimal

from kanakko import handlers
from kanakko.db import account_balances, create_household_of_one, get_or_create_user
from kanakko.handlers import TextMessage, handle_account
from kanakko.migrate import migrate

USER_TG = 501


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    return sent


def _account(text, from_id=USER_TG):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _seed(conn):
    user_id = get_or_create_user(conn, USER_TG)
    create_household_of_one(conn, user_id)
    return user_id


def test_bare_command_is_a_usage_hint_and_stores_nothing(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account"))
    assert result is None
    assert sent == [(USER_TG, handlers.ACCOUNT_USAGE)]
    assert {a["kind"] for a in account_balances(conn, user_id)} == {"spending", "external"}
    conn.rollback()


def test_unknown_kind_is_refused(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account cash 500"))
    assert result is None
    assert sent == [(USER_TG, handlers.ACCOUNT_BAD_KIND)]
    assert {a["kind"] for a in account_balances(conn, user_id)} == {"spending", "external"}
    conn.rollback()


def test_unparseable_amount_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account credit not-a-number"))
    assert result is None
    assert sent == [(USER_TG, handlers.ACCOUNT_BAD_AMOUNT)]
    conn.rollback()


def test_credit_amount_is_stored_negated_and_reads_back_as_owed(conn, monkeypatch):
    """§18: "same column, different question" — the user reports what they owe
    (positive), and the account must read back as owed (positive), not as −5000
    held. Dropping the negation in `set_account_opening_balance` reddens this.
    """
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account credit 5000"))
    assert result["kind"] == "credit" and result["name"] == "Card"
    assert sent == [(USER_TG, "Got it — Card (credit), you owe ₹5,000.00.")]
    balances = {a["account_id"]: a for a in account_balances(conn, user_id)}
    assert balances[result["account_id"]]["balance"] == Decimal("5000.00")
    conn.rollback()


def test_locked_amount_is_stored_as_a_positive_asset(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account locked 20000"))
    assert result["kind"] == "locked" and result["name"] == "Savings"
    balances = {a["account_id"]: a for a in account_balances(conn, user_id)}
    assert balances[result["account_id"]]["balance"] == Decimal("20000.00")
    conn.rollback()


def test_no_household_is_refused_not_a_crash(conn, monkeypatch):
    """Open signup mode admits a user before `/start` mints a household
    (`create_household_of_one`), so their first message can be `/account
    credit 5000` with no `household_members` row yet. Unpacking the INSERT's
    `RETURNING` unconditionally raises `TypeError: cannot unpack non-iterable
    NoneType object` — reddens without the `row is None` guard.
    """
    migrate(conn)
    get_or_create_user(conn, USER_TG)  # no create_household_of_one
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account credit 5000"))
    assert result is None
    assert sent == [(USER_TG, handlers.ACCOUNT_NO_HOUSEHOLD)]
    conn.rollback()


def test_repeat_call_updates_the_same_account_not_a_duplicate(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    _stub_send(monkeypatch)
    first = handle_account(conn, _account("/account credit 5000"))
    second = handle_account(conn, _account("/account credit 4500"))
    assert second["account_id"] == first["account_id"]
    credit_accounts = [a for a in account_balances(conn, user_id) if a["kind"] == "credit"]
    assert len(credit_accounts) == 1
    assert credit_accounts[0]["balance"] == Decimal("4500.00")
    conn.rollback()
