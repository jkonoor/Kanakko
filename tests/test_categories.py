"""The category set is the enum the model must match, the value stored, and the
button label shown — all three. A typo here splits a spending breakdown silently
(`Food` vs `food`, DECISIONS §11), so the names are asserted verbatim against the
spec, and the keyboard is asserted to mirror the constant it is generated from.
"""

from telegram import InlineKeyboardMarkup

from kanakko.categories import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    keyboard,
    schema_enum,
)

# DECISIONS §11, transcribed by hand from the spec — the point of the guard is
# that these are NOT imported from the module under test. §18 amends §11 by
# removing `Refund` from the income list (a refund is negative spending, not
# income — it now has its own `transaction` type, not a category).
SPEC_EXPENSE = [
    "Food",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Utilities",
    "Health",
    "Entertainment",
    "Other",
]
SPEC_INCOME = ["Salary", "Freelance", "Other"]


def test_categories_match_the_spec_verbatim():
    assert list(EXPENSE_CATEGORIES) == SPEC_EXPENSE
    assert list(INCOME_CATEGORIES) == SPEC_INCOME


def test_schema_enum_is_the_deduped_union():
    # "Other" is in both lists but must appear once, or the model sees a dupe.
    assert schema_enum() == SPEC_EXPENSE + ["Salary", "Freelance"]
    assert schema_enum().count("Other") == 1


def test_keyboard_mirrors_the_constant():
    for txn_type, cats in (("expense", SPEC_EXPENSE), ("income", SPEC_INCOME)):
        kb = keyboard(txn_type)
        assert isinstance(kb, InlineKeyboardMarkup)
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert [b.text for b in buttons] == cats
        assert [b.callback_data for b in buttons] == [f"cat:{c}" for c in cats]
        assert all(len(row) <= 2 for row in kb.inline_keyboard)
