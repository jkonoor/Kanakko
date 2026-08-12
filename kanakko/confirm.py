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

# callback_data prefix for an account button (§18) — `acct:<name>`, the same
# shape as `categories.CATEGORY_PREFIX`.
ACCOUNT_PREFIX = "acct:"

# callback_data for the "Change amount" button on a recurring-rule card (§18).
# Not a prefix like ACCOUNT_PREFIX/CATEGORY_PREFIX — there is only ever one of
# these per card, so an exact match is enough.
CHANGE_AMOUNT = "change_amount"

# `jobs.recurring`'s `cancel_label` and `handlers.handle_amount_reply`'s
# re-render both need this exact string, so it lives here once rather than in
# `jobs/recurring.py` where only the cron send used to read it.
SKIP_LABEL = "⏭️ Skip"


def account_keyboard(accounts: list[str]) -> InlineKeyboardMarkup:
    """Account buttons, two per row — the same shape as `categories.keyboard` (§18, §5).

    Only meaningful when there is a real choice to show; callers gate on
    `len(accounts) > 1` (§18: "accounts become visible only when a second one
    exists") before calling this.
    """
    rows = [
        [
            InlineKeyboardButton(a, callback_data=f"{ACCOUNT_PREFIX}{a}")
            for a in accounts[i : i + 2]
        ]
        for i in range(0, len(accounts), 2)
    ]
    return InlineKeyboardMarkup(rows)


def confirm_card(
    txn: Transaction,
    accounts: list[str] | None = None,
    cancel_label: str = "❌ Cancel",
    change_amount_button: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
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

    §18: the account line and its `acct:<name>` buttons only appear when
    `accounts` names a real choice — `len(accounts) > 1`, the same gate
    `parse_schema` uses. A single-account household (the daily path) gets
    exactly the pre-accounts card: no extra line, no extra tap. `accounts` is
    ordered default-first (`db.list_accounts`), so a null `txn.account` (§18:
    "null means the default account") displays as `accounts[0]`.

    §18: a `transfer` has no category — it is neither spending nor income — so
    it renders its two account names instead ("Bank → Card") and skips the
    category line, buttons and account picker entirely; Cancel-and-retype is
    the correction path for a wrong transfer, not a second chooser.

    §18 (investment accounts): a transfer naming `new_locked_account` instead of
    `to_account` (the pool doesn't exist yet — `confirm_pending` mints it on
    Confirm) leads with the question the task names: "New savings account
    'SIP'?" — Confirm/Cancel doubling as create/decline, no separate tap.

    §18 (recurring rules): `cancel_label` swaps the second button's text —
    `jobs.recurring` passes "⏭️ Skip" so a cron-sent card reads "Confirm /
    Skip", matching the spec's wording, without a second callback_data or a
    second handler: `callback_data=CANCEL` is unchanged, so the tap still
    routes through the ordinary `handle_cancel`, which is exactly what
    skipping this month's send means — discard the pending row, leave the rule
    itself alone. `change_amount_button` adds the third button the spec names
    ("Confirm / Change amount / Skip") on its own row, below Confirm/Skip and
    above the category buttons — `jobs.recurring` is the only caller that
    passes it, and `handlers.handle_amount_reply` passes it again when
    re-rendering the card so the button survives the edit.
    """
    if txn.type == "transfer":
        to_display = txn.to_account or txn.new_locked_account
        lines = []
        if txn.new_locked_account:
            lines.append(f'New savings account "{txn.new_locked_account}"?')
        lines += [
            f"Transfer — {format_amount(txn.amount)}",
            f"{txn.from_account} → {to_display}",
            f"Date: {txn.date.isoformat()}",
            f"Note: {txn.note}",
        ]
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm", callback_data=CONFIRM),
                InlineKeyboardButton(cancel_label, callback_data=CANCEL),
            ]]
        )
        return "\n".join(lines), keyboard
    assert txn.category is not None, "null category must route to category_prompt (§3)"
    lines = [
        f"{txn.type.capitalize()} — {format_amount(txn.amount)}",
        f"Category: {txn.category}",
        f"Date: {txn.date.isoformat()}",
        f"Note: {txn.note}",
    ]
    show_accounts = bool(accounts) and len(accounts) > 1
    if show_accounts:
        lines.insert(2, f"Account: {txn.account or accounts[0]}")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=CONFIRM),
                InlineKeyboardButton(cancel_label, callback_data=CANCEL),
            ],
            *([[InlineKeyboardButton("✏️ Change amount", callback_data=CHANGE_AMOUNT)]]
              if change_amount_button else []),
            *category_keyboard(txn.type).inline_keyboard,
            *(account_keyboard(accounts).inline_keyboard if show_accounts else []),
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
    A `transfer` row (§18) carries `from_account`/`to_account`, not a category, so
    it renders its two account names instead of `Category: None`.
    """
    if row["type"] == "transfer":
        return "\n".join(
            [
                f"✅ Saved — Transfer {format_amount(row['amount'])}",
                f"{row['from_account']} → {row['to_account']}",
                f"Date: {row['occurred_on'].isoformat()}",
                f"Note: {row['note']}",
            ]
        )
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
