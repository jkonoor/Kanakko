"""The recent-transactions list: per-row delete, category, and field edit
(§13, §18, tasks 100, 101, 974).

Split off `render.py` — that module is the period panels, this is the list
underneath them. The note is the one user-typed string that reaches this
markup and is HTML-escaped here; nothing else is.
"""

import html
from decimal import Decimal

from kanakko.categories import CATEGORIES_BY_TYPE
from kanakko.money import format_amount


def _category_select(txn_id: int, type_: str, current: str | None) -> str:
    """A per-row category `<select>` for the recent list (§5, §13, task 101).

    Offers the closed set for this transaction's `type_` (`categories.py`), the
    current category pre-selected. A null/unknown category shows a disabled
    "Uncategorised" placeholder so the picker still names one in one change. The
    option *value* the browser reports is the decoded name (`html.escape` only
    affects rendering), so `POST /app/category` receives the literal category. The
    `data-id` carries the `txn_id`. Category names come from the fixed set today,
    but escaping keeps the markup safe if a user-named category ever reaches it.
    """
    known = current in CATEGORIES_BY_TYPE.get(type_, ())
    opts = []
    if not known:
        opts.append('<option value="" disabled selected>Uncategorised</option>')
    for c in CATEGORIES_BY_TYPE.get(type_, ()):
        sel = " selected" if c == current else ""
        opts.append(f"<option{sel}>{html.escape(c)}</option>")
    return (
        f'<select class="cat-select" data-id="{txn_id}" aria-label="Category">'
        + "".join(opts)
        + "</select>"
    )


def _account_select(txn_id: int, accounts: list[tuple[int, str]], current: int | None) -> str:
    """A per-row account `<select>` for the recent list (§13, §18, task 974).

    Offers the household's live, non-`external` accounts (`db.household_accounts`
    — `external` is structural, never something a user picks, same rule
    `list_accounts` applies to the parse schema), the row's current `account_id`
    pre-selected. `data-id` carries the `txn_id`, `data-edit-field="account_id"`
    is the shared hook `shell.py`'s change handler dispatches on to build the
    `POST /app/edit` body — the same attribute every other edit control in this
    row carries. Only called for a non-`transfer` row with more than one live
    account: a transfer names two ends, not one (§18), so it has no single
    account to reassign here, and a single-account household has nothing to
    choose between — the same `show_accounts` gate `confirm.py` applies to the
    confirm card.
    """
    opts = "".join(
        f'<option value="{account_id}"{" selected" if account_id == current else ""}>'
        f'{html.escape(name)}</option>'
        for account_id, name in accounts
    )
    return (
        f'<select class="edit-account" data-id="{txn_id}" data-edit-field="account_id" '
        f'aria-label="Account">{opts}</select>'
    )


def _edit_panel(
    txn_id: int,
    type_: str,
    amount: Decimal,
    note: str | None,
    occurred_on,
    account_id: int | None,
    accounts: list[tuple[int, str]],
) -> str:
    """The hidden per-row editor: amount, date, note, and (non-transfer) account
    (§13, §18, task 974).

    §5 rejected a field editor in the *bot chat* — a conversation state machine —
    but named this Mini App list as the cheaper replacement for anything older
    than the last entry, so this is that editor. Hidden by default (the `hidden`
    attribute, the same mechanism the period panels already use) and revealed by
    the row's `.edit-toggle` button; nothing here changes the always-visible
    summary line, so none of that markup's existing tests move. Every control
    shares `data-id` and a `data-edit-field` naming the column it changes —
    `shell.py`'s one `change` handler POSTs `{id, field, value}` to
    `POST /app/edit` for all four, mirroring `edit_transaction_field` accepting
    one column name rather than four near-duplicate functions. Native inputs
    throughout (`type="date"`, `type="number"`) — no picker library (§13, §7).
    The amount value is the plain `Decimal` string ("50.00"), not `format_amount`'s
    ₹-and-commas form, since this is the value a number input edits, not displays.
    """
    account_field = (
        _account_select(txn_id, accounts, account_id)
        if type_ != "transfer" and len(accounts) > 1
        else ""
    )
    return (
        f'<div class="txn-edit" hidden data-id="{txn_id}">'
        f'<input type="date" class="edit-date" data-id="{txn_id}" '
        f'data-edit-field="occurred_on" aria-label="Date" value="{occurred_on.isoformat()}">'
        f'<input type="number" step="0.01" min="0.01" class="edit-amount" data-id="{txn_id}" '
        f'data-edit-field="amount" aria-label="Amount" value="{amount}">'
        f'<input type="text" class="edit-note" data-id="{txn_id}" data-edit-field="note" '
        f'aria-label="Note" placeholder="Note" value="{html.escape(note or "")}">'
        + account_field
        + "</div>"
    )


def _refund_panel(txn_id: int, amount: Decimal) -> str:
    """The hidden per-row refund input for an `expense` row (§13, §16, §18, task 1082).

    Unlike `_edit_panel`'s fields, which auto-save on `change`, this carries its
    own explicit submit button: a refund *adds* a new row rather than overwriting
    one, so silently firing on blur would create a transaction the user never
    meant to confirm — the same explicitness the bot's button tap has
    (`handlers.handle_refund_choice`). The amount input defaults to the row's
    full original amount; `POST /app/refund` (not this panel) is what enforces
    that a refund can't exceed what's left, via migration 014's trigger, the same
    guard the bot side relies on.
    """
    return (
        f'<div class="txn-refund" hidden data-id="{txn_id}">'
        f'<input type="number" step="0.01" min="0.01" class="refund-amount" '
        f'data-id="{txn_id}" aria-label="Refund amount" value="{amount}">'
        f'<button type="button" class="refund-submit" data-id="{txn_id}">Refund</button>'
        "</div>"
    )


def recent_list(rows: list[tuple], accounts: list[tuple[int, str]] = ()) -> str:
    """The recent-transactions list: per-row delete, category, and field edit
    (§13, §18, tasks 100, 101, 974).

    Each `rows` entry is `(txn_id, amount, type, category, note, occurred_on,
    from_account, to_account, account_id)` from `db.recent_transactions` —
    `from_account`/`to_account` are `NULL` for every type but `transfer`, and
    `account_id` is `NULL` for exactly that one type and nothing else (§18).
    `note` is the first *user-typed* string the dashboard renders — §11 keeps the
    note's original wording, so it is arbitrary text that arrived through the bot
    — and it is HTML-escaped: an unescaped `<img src=x onerror=...>` in a logged
    expense would be stored XSS. A `transfer` has no category (it is excluded
    from both totals, §18) so it renders its two account names — "Bank → SIP" —
    instead of the category `<select>`, and carries neither the −/+ sign nor the
    income tint, since it is neither spending nor income. Every other row keeps
    the existing category `<select>` (a null category is "Uncategorised") that
    POSTs to `/app/category`, and its −/+ sign by direction. Amounts go through
    `format_amount` (§9) on the summary line. `accounts` is the household's live,
    non-`external` accounts (`db.household_accounts`) — the same list for every
    row, so the caller fetches it once, not once per row. The delete button
    carries the `txn_id` for `POST /app/delete`; the edit toggle reveals the
    hidden per-row editor built by `_edit_panel`. An `expense` row additionally
    gets a refund toggle, revealing `_refund_panel` — nothing else is refundable
    (§18, task 1082): a `transfer` moves money rather than spending it, an
    `income`/`refund` row is not an expense to refund, and `create_refund` itself
    only accepts a live `expense`. An empty ledger renders nothing.
    """
    if not rows:
        return ""
    items = []
    for txn_id, amount, type_, category, note, occurred_on, from_account, to_account, account_id in rows:
        if type_ == "transfer":
            sign = ""
            amt_cls = "amt"
            detail = (
                f'<span class="txn-transfer">{html.escape(from_account or "?")}'
                f' → {html.escape(to_account or "?")}</span>'
            )
        else:
            sign = "−" if type_ == "expense" else "+"
            # The one accent: income reads as positive at a glance. The sign
            # carries the same meaning, so colour is never the sole signal
            # (WCAG 1.4.1).
            amt_cls = "amt" if type_ == "expense" else "amt in"
            detail = _category_select(txn_id, type_, category)
        note_html = (
            f'<div class="txn-note">{html.escape(note)}</div>' if note else ""
        )
        items.append(
            '<div class="txn">'
            f'<div class="txn-main"><span>{occurred_on:%d %b} · '
            + detail
            + f'</span><span class="{amt_cls}">{sign}{format_amount(amount)}</span></div>'
            + note_html
            # aria-label because the glyph alone announces as "✕" to a screen
            # reader — Telegram's design guidelines require that inputs and
            # images carry labels, and this button deletes a real transaction.
            + f'<button type="button" class="del" data-id="{txn_id}" '
            f'aria-label="Delete {sign}{format_amount(amount)} on '
            f'{occurred_on:%d %b}">✕</button>'
            f'<button type="button" class="edit-toggle" data-id="{txn_id}" '
            f'aria-label="Edit {sign}{format_amount(amount)} on '
            f'{occurred_on:%d %b}">✎</button>'
            + (
                f'<button type="button" class="refund-toggle" data-id="{txn_id}" '
                f'aria-label="Refund {format_amount(amount)} on '
                f'{occurred_on:%d %b}">↩</button>'
                if type_ == "expense" else ""
            )
            + _edit_panel(txn_id, type_, amount, note, occurred_on, account_id, accounts)
            + (_refund_panel(txn_id, amount) if type_ == "expense" else "")
            + "</div>"
        )
    return '<section class="recent"><h2>Recent</h2>' + "".join(items) + "</section>"
