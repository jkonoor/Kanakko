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

from conftest import household_of

from kanakko.commands import account as account_command
from kanakko.commands.account import handle_account
from kanakko.db import account_balances, create_household_of_one, get_or_create_user
from kanakko.handlers import TextMessage
from kanakko.migrate import migrate

USER_TG = 501


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(account_command, "send_message",
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
    assert sent == [(USER_TG, account_command.ACCOUNT_USAGE)]
    assert {a["kind"] for a in account_balances(conn, user_id)} == {"spending", "external"}
    conn.rollback()


def test_unknown_kind_is_refused(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account cash 500"))
    assert result is None
    assert sent == [(USER_TG, account_command.ACCOUNT_BAD_KIND)]
    assert {a["kind"] for a in account_balances(conn, user_id)} == {"spending", "external"}
    conn.rollback()


def test_unparseable_amount_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_account(conn, _account("/account credit not-a-number"))
    assert result is None
    assert sent == [(USER_TG, account_command.ACCOUNT_BAD_AMOUNT)]
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
    assert sent == [(USER_TG, account_command.ACCOUNT_NO_HOUSEHOLD)]
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


def test_bare_name_reports_a_locked_accounts_contributions_and_payouts(conn, monkeypatch):
    """`/account <name>` — one bare word — is the read side (§18): gross in/out,
    matched case-insensitively against a `locked` account's name, plus the
    opening balance it was onboarded with, so "put in ₹0.00" doesn't read as
    the starting ₹1,000 having vanished.
    """
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    handle_account(conn, _account("/account locked 1000"))  # mints "Savings"
    sent.clear()

    result = handle_account(conn, _account("/account savings"))  # lowercase, matches "Savings"
    assert result["name"] == "Savings"
    assert sent == [(USER_TG,
                     "Savings — put in ₹0.00, got back ₹0.00, started with ₹1,000.00.")]
    conn.rollback()


def test_bare_name_with_no_opening_balance_omits_the_started_with_clause(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    hh = household_of(conn, user_id)
    with conn.cursor() as cur:
        # Standing in for an auto-created pool (`new_locked_account`), which is
        # minted with no opening balance — only `/account locked <amount>`
        # onboarding ever sets one.
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'locked', 'Savings')",
            (hh, user_id),
        )
    sent = _stub_send(monkeypatch)

    handle_account(conn, _account("/account savings"))
    assert sent == [(USER_TG, "Savings — put in ₹0.00, got back ₹0.00.")]
    conn.rollback()


def test_bare_name_with_no_matching_locked_account_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)

    result = handle_account(conn, _account("/account SIP"))

    assert result is None
    assert sent == [(USER_TG, account_command.ACCOUNT_NOT_LOCKED.format(name="SIP"))]
    conn.rollback()


def test_multi_word_locked_account_name_is_reachable_as_a_query(conn, monkeypatch):
    """F1: a `locked` account auto-created with a multi-word name (an escape
    valve `new_locked_account` offers, e.g. "Kids Fund") must still be
    queryable — the first word alone ("Kids") isn't `credit`/`locked`, so the
    whole argument, not just the first word, is read as the account name.
    """
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    handle_account(conn, _account("/account locked 500"))
    # Rename the minted account to a multi-word name, standing in for one
    # `confirm_pending`'s get-or-create would mint from natural language.
    with conn.cursor() as cur:
        cur.execute("UPDATE accounts SET name = 'Kids Fund' WHERE name = 'Savings'")
    sent.clear()

    result = handle_account(conn, _account("/account Kids Fund"))

    assert result is not None and result["name"] == "Kids Fund"
    assert sent == [(USER_TG,
                     "Kids Fund — put in ₹0.00, got back ₹0.00, started with ₹500.00.")]
    conn.rollback()


def test_year_suffixed_locked_account_name_is_reachable_as_a_query(conn, monkeypatch):
    """F4: a `locked` account auto-created with a name that happens to end in
    something that parses as an amount ("Goa 2026") must still be queryable —
    the query lookup runs before the bad-kind heuristic, so an existing account
    wins over the "cash 500" botched-onboarding guess.
    """
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    handle_account(conn, _account("/account locked 500"))
    with conn.cursor() as cur:
        cur.execute("UPDATE accounts SET name = 'Goa 2026' WHERE name = 'Savings'")
    sent.clear()

    result = handle_account(conn, _account("/account Goa 2026"))

    assert result is not None and result["name"] == "Goa 2026"
    assert sent == [(USER_TG,
                     "Goa 2026 — put in ₹0.00, got back ₹0.00, started with ₹500.00.")]
    conn.rollback()


def test_two_word_bad_kind_attempt_is_still_refused_as_a_bad_kind(conn, monkeypatch):
    """F1's fix must not swallow the existing "unknown kind" refusal: a
    two-word attempt whose second word looks like an amount ("cash 500") is a
    botched onboarding call, not a query for an account literally named
    "cash 500".
    """
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)

    result = handle_account(conn, _account("/account cash 500"))

    assert result is None
    assert sent == [(USER_TG, account_command.ACCOUNT_BAD_KIND)]
    assert {a["kind"] for a in account_balances(conn, user_id)} == {"spending", "external"}
    conn.rollback()


def test_credit_or_locked_with_no_amount_is_a_usage_hint_not_a_query(conn, monkeypatch):
    """F3: `/account credit` (amount forgotten) must fall through to the usage
    hint, not be read as a query for a `locked` account literally named
    "credit" — the pre-F1 behaviour this regressed when the query path first
    landed.
    """
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)

    result = handle_account(conn, _account("/account credit"))

    assert result is None
    assert sent == [(USER_TG, account_command.ACCOUNT_USAGE)]
    conn.rollback()
