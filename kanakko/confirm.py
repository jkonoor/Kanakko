"""The confirm card shown before every transaction is stored (DECISIONS §4, §5).

Pure rendering: a validated `Transaction` in, the card text and its inline
keyboard out. Sending it, persisting the pending row, and handling the button
presses are the following tasks. Amount is rendered through `money.format_amount`
so the card can never show a float (§9), and the Confirm/Cancel `callback_data`
are the constants the button handlers route on.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.categories import keyboard as category_keyboard
from kanakko.money import format_amount
from kanakko.parse import Transaction

# callback_data the Confirm/Cancel handlers (tasks 56, 57) match on. The webhook
# dispatch already carries these back as a ButtonPress.data.
CONFIRM = "confirm"
CANCEL = "cancel"


def confirm_card(txn: Transaction) -> tuple[str, InlineKeyboardMarkup]:
    """Render `txn` as the confirm-card text plus its Confirm/Cancel keyboard.

    §4: every parsed transaction shows this before it is stored. A null category
    routes to `category_prompt` instead (§3), so a confirm card always has a
    category to name — the assert makes that precondition fail loud rather than
    render the literal `Category: None`. Plain text, no `parse_mode` — the note
    is the user's own wording and must not need Markdown/HTML escaping.

    §5: the category buttons sit directly on the card, below Confirm/Cancel —
    category is the most-often-wrong field and a closed set, so correcting it is
    one tap (`cat:<name>`, handled by the category-press task) rather than a
    Cancel-and-retype.
    """
    assert txn.category is not None, "null category must route to category_prompt (§3)"
    lines = [
        f"{txn.type.capitalize()} — {format_amount(txn.amount)}",
        f"Category: {txn.category}",
        f"Date: {txn.date.isoformat()}",
        f"Note: {txn.note}",
    ]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=CANCEL),
            ],
            *category_keyboard(txn.type).inline_keyboard,
        ]
    )
    return "\n".join(lines), keyboard


def settled_card(row: dict) -> str:
    """Render a stored transaction as a settled receipt — text only, no keyboard (§4, §5).

    `handle_confirm` edits the confirm card into this once the row is stored, so the
    transcript keeps a permanent receipt instead of only a toast that fades. Takes
    the row `confirm_pending` returns — a Decimal `amount` and a `occurred_on`
    date, the ledger's own shape, not a `Transaction`. Amount goes through
    `format_amount` so a settled card can no more show a float than a live one (§9).
    Deliberately returns no keyboard: the entry is saved, so there is nothing left
    to Confirm or Cancel, and a stale Cancel on this card must find no live button.
    """
    return "\n".join(
        [
            f"✅ Saved — {row['type'].capitalize()} {format_amount(row['amount'])}",
            f"Category: {row['category']}",
            f"Date: {row['occurred_on'].isoformat()}",
            f"Note: {row['note']}",
        ]
    )


def category_prompt(txn: Transaction) -> tuple[str, InlineKeyboardMarkup]:
    """Render the category picker shown when the model returned no category (§3, §5).

    `category: null` means the model genuinely couldn't tell (§3), so instead of a
    confirm card with a blank in the category field we show the closed set as
    buttons and ask the user to pick — one tap, no free text. The amount/type/note
    are still shown so the choice is in context. The buttons carry `cat:<name>`
    (`categories.keyboard`) for the category-press handler to route on.
    """
    lines = [
        f"{txn.type.capitalize()} — {format_amount(txn.amount)}",
        f"Note: {txn.note}",
        "Which category?",
    ]
    return "\n".join(lines), category_keyboard(txn.type)
