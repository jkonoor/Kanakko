"""`/recurring`: the creation surface for auto-debit rules (§18).

`db.recurring.create_recurring_rule`/`list_recurring_rules`/`set_recurring_rule_active`/
`delete_recurring_rule` (task 1125) shipped with no way to actually mint a rule
short of raw SQL — this is that surface, in the `/account`-style shape:
`/recurring <amount> <day> <category> <account>`. `amount` and `day` anchor the
front of the command since both are unambiguous; `category`+`account` share the
rest of the text with no delimiter between them, so the guard this proves is
that the split still lands correctly when either is more than one word.
"""

from decimal import Decimal

from conftest import household_of

from kanakko.commands import recurring as recurring_command
from kanakko.commands.recurring import handle_recurring
from kanakko.db import get_or_create_user, list_recurring_rules
from kanakko.handlers import TextMessage
from kanakko.migrate import migrate

USER_TG = 601


def _stub_send(monkeypatch):
    sent = []
    monkeypatch.setattr(recurring_command, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    return sent


def _recurring(text, from_id=USER_TG):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def _seed(conn):
    user_id = get_or_create_user(conn, USER_TG)
    household_of(conn, user_id)  # mints the default "Bank" spending account
    return user_id


def test_bare_command_is_a_usage_hint_and_stores_nothing(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring"))
    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_USAGE)]
    assert list_recurring_rules(conn, user_id) == []
    conn.rollback()


def test_too_few_arguments_is_a_usage_hint(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring 5000 5"))
    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_USAGE)]
    conn.rollback()


def test_unparseable_amount_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring not-a-number 5 Food Bank"))
    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_BAD_AMOUNT)]
    conn.rollback()


def test_out_of_range_day_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring 5000 32 Food Bank"))
    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_BAD_DAY)]
    conn.rollback()


def test_non_numeric_day_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring 5000 five Food Bank"))
    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_BAD_DAY)]
    conn.rollback()


def test_unmatched_category_and_account_is_refused(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _stub_send(monkeypatch)
    result = handle_recurring(conn, _recurring("/recurring 5000 5 Nonsense Nowhere"))
    assert result is None
    assert sent == [(
        USER_TG,
        recurring_command.RECURRING_BAD_CATEGORY_ACCOUNT.format(
            rest="Nonsense Nowhere",
            categories=", ".join(recurring_command.EXPENSE_CATEGORIES),
        ),
    )]
    conn.rollback()


def test_good_call_creates_an_active_rule_on_the_named_account(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    sent = _stub_send(monkeypatch)

    result = handle_recurring(conn, _recurring("/recurring 5000 5 Food Bank"))

    assert result["category"] == "Food"
    assert result["amount"] == Decimal("5000")
    assert result["day_of_month"] == 5
    assert result["active"] is True
    assert sent == [(
        USER_TG,
        "Got it — ₹5,000.00 for Food from Bank on day 5 of the month. "
        "Manage it from Dashboard.",
    )]
    rules = list_recurring_rules(conn, user_id)
    assert len(rules) == 1
    assert rules[0]["account_name"] == "Bank"
    conn.rollback()


def test_multi_word_category_is_split_from_the_account_correctly(conn, monkeypatch):
    """"Bills & Utilities" — a multi-word category — must not be misread as part
    of the account name, and vice versa. This is the guard the brute-force
    prefix match in `_match_category_and_account` exists for: a positional
    split (first N words = category) would grab "Bills & Utilities Bank" as one
    lump the moment the account is itself more than one word.
    """
    migrate(conn)
    user_id = _seed(conn)
    _stub_send(monkeypatch)

    result = handle_recurring(conn, _recurring("/recurring 1200 1 Bills & Utilities Bank"))

    assert result["category"] == "Bills & Utilities"
    rules = list_recurring_rules(conn, user_id)
    assert rules[0]["category"] == "Bills & Utilities"
    assert rules[0]["account_name"] == "Bank"
    conn.rollback()


def test_multi_word_account_name_is_matched(conn, monkeypatch):
    migrate(conn)
    user_id = _seed(conn)
    hh = household_of(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name)"
            " VALUES (%s, %s, 'locked', 'Kids Fund')",
            (hh, user_id),
        )
    _stub_send(monkeypatch)

    result = handle_recurring(conn, _recurring("/recurring 500 10 Shopping Kids Fund"))

    assert result["category"] == "Shopping"
    rules = list_recurring_rules(conn, user_id)
    assert any(r["account_name"] == "Kids Fund" for r in rules)
    conn.rollback()


def test_no_household_is_refused_not_a_crash(conn, monkeypatch):
    migrate(conn)
    get_or_create_user(conn, USER_TG)  # no household_of
    sent = _stub_send(monkeypatch)

    result = handle_recurring(conn, _recurring("/recurring 5000 5 Food Bank"))

    assert result is None
    assert sent == [(USER_TG, recurring_command.RECURRING_NO_HOUSEHOLD)]
    conn.rollback()
