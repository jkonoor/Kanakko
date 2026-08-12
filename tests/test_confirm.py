"""The confirm card renders a Transaction into card text + Confirm/Cancel (§4, §5).

Two things are guarded: the amount is rendered through `format_amount` (so a card
never shows a bare float — the §9 money guard), and the two buttons carry the
exact `callback_data` the Confirm/Cancel handlers (tasks 56, 57) route on.
"""

from datetime import date
from decimal import Decimal

from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.confirm import ACCOUNT_PREFIX, CANCEL, CONFIRM, confirm_card, settled_card
from kanakko.parse import Transaction


def _txn(**over) -> Transaction:
    fields = {
        "type": "expense",
        "amount": Decimal("1234.5"),
        "category": "Food",
        "date": date(2026, 8, 6),
        "note": "lunch at the mess",
    }
    fields.update(over)
    return Transaction(**fields)


def test_card_shows_every_field():
    text, _ = confirm_card(_txn())
    assert "Expense" in text
    assert "Category: Food" in text
    assert "Date: 2026-08-06" in text
    assert "Note: lunch at the mess" in text


def test_amount_is_formatted_not_a_bare_number():
    # The §9 guard: the amount goes through format_amount, so the card shows
    # "₹1,234.50" — grouped, ₹-prefixed, two decimals. A raw Decimal/float would
    # render "1234.5" with no ₹ and no grouping. If someone bypasses
    # format_amount this assertion reddens.
    text, _ = confirm_card(_txn(amount=Decimal("1234.5")))
    assert "₹1,234.50" in text
    assert "1234.5" not in text  # the unformatted form must not leak through


def test_income_type_is_titled():
    text, _ = confirm_card(_txn(type="income", category="Salary"))
    assert text.startswith("Income — ")


def test_buttons_are_confirm_and_cancel_with_routing_data():
    _, keyboard = confirm_card(_txn())
    row = keyboard.inline_keyboard[0]
    assert [b.callback_data for b in row] == [CONFIRM, CANCEL]
    assert (CONFIRM, CANCEL) == ("confirm", "cancel")  # the dispatch's contract
    assert "Confirm" in row[0].text and "Cancel" in row[1].text


def test_category_buttons_are_on_the_card_for_one_tap_correction():
    # §5.1: the wrong-category fix must be one tap on the card itself, not a
    # Cancel-and-retype. The card carries every category of the txn's type, each
    # with `cat:<name>` routing data, below the Confirm/Cancel row. If someone
    # drops the category buttons back off the card this reddens.
    _, keyboard = confirm_card(_txn(type="expense", category="Food"))
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert set(f"cat:{c}" for c in EXPENSE_CATEGORIES) <= set(data)
    # ...and they come from categories.py, not a literal list living here.
    assert "cat:Food" in data and "cat:Bills & Utilities" in data


def test_no_account_line_or_buttons_without_a_real_choice():
    # §18: "accounts become visible only when a second one exists" — the daily
    # path (no accounts, or exactly one) must not gain a line or a tap. This is
    # the check the task names: a one-account household still confirms in one tap.
    for accounts in (None, [], ["Bank"]):
        text, keyboard = confirm_card(_txn(), accounts)
        assert "Account:" not in text
        data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
        assert not any(d.startswith(ACCOUNT_PREFIX) for d in data)


def test_account_line_and_buttons_appear_once_a_second_account_exists():
    # A real choice: the card names the account the parse chose (or the default,
    # when null) and offers every account as an `acct:<name>` button, the same
    # `cat:<name>` shape §5 already uses for category — reusing the pattern, not
    # a second chooser.
    text, keyboard = confirm_card(_txn(account=None), ["Bank", "Card"])
    assert "Account: Bank" in text  # null account displays as the default (first)
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert {"acct:Bank", "acct:Card"} <= set(data)

    text, _ = confirm_card(_txn(account="Card"), ["Bank", "Card"])
    assert "Account: Card" in text  # a chosen account displays as itself


def test_transfer_card_shows_no_category_and_no_pickers():
    # §18: a transfer is neither spending nor income, so it has no category and
    # renders its two account names instead — no category buttons, no account
    # picker (the accounts are already fixed by the parse), just Confirm/Cancel.
    txn = _txn(type="transfer", category=None, from_account="Bank", to_account="Card")
    text, keyboard = confirm_card(txn, ["Bank", "Card"])
    assert "Transfer" in text
    assert "Bank → Card" in text
    assert "Category:" not in text
    assert "Account:" not in text
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert data == [CONFIRM, CANCEL]  # nothing else on the card


def test_new_locked_account_transfer_card_asks_before_creating_it():
    # §18 (investment accounts): "put 5000 in SIP" with no SIP account yet leads
    # with the question the task names, and shows the destination by the pool's
    # name even though `to_account` itself is still null (confirm_pending mints
    # the account on Confirm — same Confirm/Cancel buttons, no extra tap).
    txn = _txn(
        type="transfer", category=None, from_account="Bank", to_account=None,
        new_locked_account="SIP",
    )
    text, keyboard = confirm_card(txn, ["Bank"])
    assert 'New savings account "SIP"?' in text
    assert "Bank → SIP" in text
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert data == [CONFIRM, CANCEL]


def test_settled_transfer_card_shows_accounts_not_category_none():
    # settled_card's counterpart: a stored transfer row must never render
    # "Category: None" — the same gap a null category would leave on any other
    # settled receipt.
    row = {
        "amount": Decimal("2000.00"),
        "type": "transfer",
        "category": None,
        "note": "paid the card bill",
        "occurred_on": date(2026, 8, 6),
        "from_account": "Bank",
        "to_account": "Card",
    }
    text = settled_card(row)
    assert "Bank → Card" in text
    assert "Category" not in text
