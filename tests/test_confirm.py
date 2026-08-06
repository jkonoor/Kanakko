"""The confirm card renders a Transaction into card text + Confirm/Cancel (§4, §5).

Two things are guarded: the amount is rendered through `format_amount` (so a card
never shows a bare float — the §9 money guard), and the two buttons carry the
exact `callback_data` the Confirm/Cancel handlers (tasks 56, 57) route on.
"""

from datetime import date
from decimal import Decimal

from kanakko.confirm import CANCEL, CONFIRM, confirm_card
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
