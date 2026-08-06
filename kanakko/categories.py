"""The closed category set (DECISIONS §11), defined once and derived everywhere.

The two lists below are the single source of truth. The JSON-schema `enum` handed
to the LLM and the Telegram keyboard shown on the confirm card are both generated
from them, so the model can never invent a category and the buttons can never
offer one the schema would reject. Never write a category string literal in
another module — import from here.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

EXPENSE_CATEGORIES = (
    "Food",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Utilities",
    "Health",
    "Entertainment",
    "Other",
)

INCOME_CATEGORIES = (
    "Salary",
    "Freelance",
    "Refund",
    "Other",
)

CATEGORIES_BY_TYPE = {"expense": EXPENSE_CATEGORIES, "income": INCOME_CATEGORIES}

# Union across both types, order-preserving, deduped ("Other" is in both lists).
ALL_CATEGORIES = tuple(dict.fromkeys(EXPENSE_CATEGORIES + INCOME_CATEGORIES))

# callback_data prefix for a category button. The keyboard emits it and the
# webhook dispatch routes on it — one definition so the two can't drift.
CATEGORY_PREFIX = "cat:"


def schema_enum() -> list[str]:
    """The `enum` for the parse schema's `category` field (DECISIONS §2, §3)."""
    return list(ALL_CATEGORIES)


def keyboard(txn_type: str) -> InlineKeyboardMarkup:
    """Category buttons for a transaction of `txn_type`, two per row.

    Shown on the confirm card and when `category` came back null (DECISIONS §5).
    `callback_data` is `cat:<name>` so the dispatch can route the press back.
    """
    cats = CATEGORIES_BY_TYPE[txn_type]
    rows = [
        [
            InlineKeyboardButton(c, callback_data=f"{CATEGORY_PREFIX}{c}")
            for c in cats[i : i + 2]
        ]
        for i in range(0, len(cats), 2)
    ]
    return InlineKeyboardMarkup(rows)
