"""The confirm card shown before every transaction is stored (DECISIONS §4, §5).

Pure rendering: a validated `Transaction` in, the card text and its inline
keyboard out. Sending it, persisting the pending row, and handling the button
presses are the following tasks. Amount is rendered through `money.format_amount`
so the card can never show a float (§9), and the Confirm/Cancel `callback_data`
are the constants the button handlers route on.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.money import format_amount
from kanakko.parse import Transaction

# callback_data the Confirm/Cancel handlers (tasks 56, 57) match on. The webhook
# dispatch already carries these back as a ButtonPress.data.
CONFIRM = "confirm"
CANCEL = "cancel"


def confirm_card(txn: Transaction) -> tuple[str, InlineKeyboardMarkup]:
    """Render `txn` as the confirm-card text plus its Confirm/Cancel keyboard.

    §4: every parsed transaction shows this before it is stored. A null category
    routes to the category buttons instead (§3, task 59), so a card always has a
    category to name. Plain text, no `parse_mode` — the note is the user's own
    wording and must not need Markdown/HTML escaping.
    """
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
            ]
        ]
    )
    return "\n".join(lines), keyboard
