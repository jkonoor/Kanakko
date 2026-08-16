"""The recent-transactions list: per-row delete, category, and field edit
(§13, §18, tasks 100, 101, 974, 1704).

Split off `render.py` — that module is the period panels, this is the list
underneath them. The note is the one user-typed string that reaches this
markup and is HTML-escaped here; nothing else is.
"""

import html
from decimal import Decimal

from kanakko.categories import CATEGORIES_BY_TYPE
from kanakko.money import format_amount


def _field(label: str, control: str) -> str:
    """One labelled row of the row editor (task 1704): a visible text label
    left, the control right. Before this, every input carried only an
    `aria-label` — invisible to a sighted user, who saw five unlabelled grey
    boxes.
    """
    return f'<div class="field"><span class="field-label">{label}</span>{control}</div>'


def _category_select(txn_id: int, type_: str, current: str | None) -> str:
    """A per-row category `<select>` for the row editor (§5, §13, tasks 101, 1704).

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


def _category_known(type_: str, category: str | None) -> bool:
    """Whether `category` is a real option for `type_` (§13, §18, task 1823).

    A `refund` has no entry in `CATEGORIES_BY_TYPE` — it is not something a
    user picks, it is inherited from the expense it refunds (`create_refund`
    copies it verbatim) — so it is checked against `expense`'s set instead,
    the same set the original was chosen from.
    """
    lookup_type = "expense" if type_ == "refund" else type_
    return category in CATEGORIES_BY_TYPE.get(lookup_type, ())


def _account_select(txn_id: int, accounts: list[tuple[int, str]], current: int | None) -> str:
    """A per-row account `<select>` for the row editor (§13, §18, tasks 974, 1704).

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


def _refund_panel(txn_id: int, remaining: Decimal) -> str:
    """The hidden per-row refund input for an `expense` row (§13, §16, §18, tasks 1082, 1799).

    Unlike the editor's fields, which auto-save on `change`, this carries its
    own explicit submit button: a refund *adds* a new row rather than overwriting
    one, so silently firing on blur would create a transaction the user never
    meant to confirm — the same explicitness the bot's button tap has
    (`handlers.handle_refund_choice`). The input starts **empty**, not
    pre-filled with the full amount — under a full-width primary button, a
    pre-filled full amount put a complete refund one tap away from opening the
    panel by accident (task 1799). `remaining` — the original amount minus any
    refunds already recorded against it, computed by `db.recent_transactions` —
    is shown as the placeholder instead: the ceiling `POST /app/refund` will
    actually accept (migration 014's trigger), not the original amount, which
    the trigger would reject once anything has already been refunded.
    """
    return (
        f'<div class="txn-refund" hidden data-id="{txn_id}">'
        f'<input type="number" step="0.01" min="0.01" max="{remaining}" class="refund-amount" '
        f'data-id="{txn_id}" aria-label="Refund amount" placeholder="{remaining}">'
        f'<button type="button" class="refund-submit" data-id="{txn_id}">Refund</button>'
        "</div>"
    )


def _txn_panel(
    txn_id: int,
    type_: str,
    amount: Decimal,
    category: str | None,
    note: str | None,
    occurred_on,
    account_id: int | None,
    accounts: list[tuple[int, str]],
    sign: str,
    remaining: Decimal,
) -> str:
    """The hidden per-row panel: correction fields, then Delete/Refund (task 1704).

    §5 rejected a field editor in the *bot chat* — a conversation state machine —
    but named this Mini App list as the cheaper replacement for anything older
    than the last entry, so this is that editor, reached by tapping the
    collapsed row rather than a separate glyph. Hidden by default and revealed
    by `.txn-head` (the same `hidden` mechanism the period panels already use).
    Every correction control shares `data-id` and a `data-edit-field` naming the
    column it changes — `shell.py`'s one `change` handler POSTs `{id, field,
    value}` to `POST /app/edit`, mirroring `edit_transaction_field` accepting one
    column name rather than four near-duplicate functions. Native inputs
    throughout (`type="date"`, `type="number"`) — no picker library (§13, §7).
    The amount value is the plain `Decimal` string ("50.00"), not `format_amount`'s
    ₹-and-commas form, since this is the value a number input edits, not displays.

    Ordered by consequence (§5: category is the most-often-wrong field) — Amount,
    Category, Date, Account, Note — and Delete/Refund sit below a rule at the
    end, separated from the fields above because those two create and destroy
    while the fields only correct. A transfer has neither a category (it is
    excluded from both totals, §18) nor a single account to reassign (it names
    two ends), so it renders only Amount, Date and Note.

    `<input type="date">` formats to the *device* locale, so a helper span
    beside it spells the date out ("06 Aug 2026") the way the summary line
    above already does — an Indian user's device would otherwise show
    `08/06/2026` under a header reading `06 Aug`.

    `remaining` (the original amount minus any refunds already recorded
    against it) is threaded through to `_refund_panel` (task 1799); it is
    `amount` itself for a never-refunded row, since `remaining` is what an
    `expense` row's panel should invite, not the original amount.
    """
    amount_field = _field(
        "Amount (₹)",
        f'<input type="number" step="0.01" min="0.01" class="edit-amount" data-id="{txn_id}" '
        f'data-edit-field="amount" aria-label="Amount" value="{amount}">',
    )
    if type_ == "transfer":
        category_field = ""
    elif type_ == "refund":
        # Inherited from the expense it refunds (`create_refund`) — editing it
        # here independently of the original would desync the category totals
        # both rows feed into, so it renders as text, not a `<select>` (task 1823).
        category_field = _field("Category", html.escape(category) if category else "Uncategorised")
    else:
        category_field = _field("Category", _category_select(txn_id, type_, category))
    date_field = _field(
        "Date",
        f'<input type="date" class="edit-date" data-id="{txn_id}" data-edit-field="occurred_on" '
        f'aria-label="Date" value="{occurred_on.isoformat()}">'
        f'<span class="date-human">{occurred_on:%d %b %Y}</span>',
    )
    account_field = (
        _field("Account", _account_select(txn_id, accounts, account_id))
        if type_ != "transfer" and len(accounts) > 1
        else ""
    )
    note_field = _field(
        "Note",
        f'<input type="text" class="edit-note" data-id="{txn_id}" data-edit-field="note" '
        f'aria-label="Note" placeholder="Note" value="{html.escape(note or "")}">',
    )
    delete_label = f"{sign}{format_amount(amount)} on {occurred_on:%d %b}"
    refund_label = f"{format_amount(amount)} on {occurred_on:%d %b}"
    actions = (
        '<div class="txn-actions">'
        f'<button type="button" class="del" data-id="{txn_id}" '
        f'data-amount="{format_amount(amount)}" aria-label="Delete {delete_label}">Delete</button>'
        + (
            f'<button type="button" class="refund-toggle" data-id="{txn_id}" '
            f'aria-label="Refund {refund_label}">Refund</button>'
            if type_ == "expense"
            else ""
        )
        + "</div>"
    )
    refund = _refund_panel(txn_id, remaining) if type_ == "expense" else ""
    return (
        f'<div class="txn-panel" hidden data-id="{txn_id}">'
        + amount_field
        + category_field
        + date_field
        + account_field
        + note_field
        + '<hr class="txn-rule">'
        + actions
        + refund
        + "</div>"
    )


def recent_list(rows: list[tuple], accounts: list[tuple[int, str]] = ()) -> str:
    """The recent-transactions list: collapsed rows that expand into an editor
    (§13, §18, tasks 100, 101, 974, 1704).

    Each `rows` entry is `(txn_id, amount, type, category, note, occurred_on,
    from_account, to_account, account_id, refunded_so_far, refund_of_note,
    refund_of_occurred_on)` from `db.recent_transactions` — `from_account`/
    `to_account` are `NULL` for every type but `transfer`, `account_id` is
    `NULL` for exactly that one type and nothing else (§18), `refunded_so_far`
    is 0 except on a partially- or fully-refunded `expense` — it is what makes
    `_refund_panel`'s placeholder (task 1799) the amount still refundable, not
    the original — and `refund_of_note`/`refund_of_occurred_on` are `NULL`
    except on a `refund` row, where they name the note and date of the expense
    it refunds (task 1823).
    `note` is the first *user-typed* string the dashboard renders — §11 keeps the
    note's original wording, so it is arbitrary text that arrived through the bot
    — and it is HTML-escaped: an unescaped `<img src=x onerror=...>` in a logged
    expense would be stored XSS. `refund_of_note` gets the same treatment.

    Task 1704 rebuilt the row after a live-UI review found the previous shape
    unusable: three always-visible action glyphs stacked one per grid row made
    every expense row at least 132px tall, showing about four transactions where
    the viewport could hold ten. The collapsed row now shows data only — date,
    category (or, for a `transfer`, "Bank → SIP" — it has none, §18), amount, and
    the note beneath, in the income accent only when it is income (WCAG 1.4.1) —
    and the whole row is one `<button>` (`.txn-head`) that expands `_txn_panel`
    on tap. Delete becoming reachable only from inside that panel is deliberate:
    two taps instead of one for a control that destroys a row. `accounts` is the
    household's live, non-`external` accounts (`db.household_accounts`) — the
    same list for every row, so the caller fetches it once, not once per row. An
    `expense` row additionally gets a refund action, revealing `_refund_panel` —
    nothing else is refundable (§18, task 1082): a `transfer` moves money rather
    than spending it, an `income`/`refund` row is not an expense to refund, and
    `create_refund` itself only accepts a live `expense`. An empty ledger renders
    nothing.

    A `refund` is neither an expense nor income (§18): it renders with a
    `transfer`-style neutral sign and colour — a live screenshot on 2026-08-16
    found it wearing the income green with "Uncategorised", the one look §18
    says a refund must never have — and its inherited category (§18,
    `create_refund`) is checked against `expense`'s set via `_category_known`,
    since `CATEGORIES_BY_TYPE` has no `refund` key of its own. Its note lane
    names what it refunds instead of showing its own (empty) `note`, and the
    expense it refunds gets a `· refunded` / `· ₹200.00 refunded` suffix on its
    own amount once anything has been taken against it, so a housemate who
    lands on either row alone can still tell what happened without hunting for
    the other one (task 1823).
    """
    if not rows:
        return ""
    items = []
    for (
        txn_id, amount, type_, category, note, occurred_on,
        from_account, to_account, account_id, refunded_so_far,
        refund_of_note, refund_of_occurred_on,
    ) in rows:
        if type_ == "transfer":
            sign = ""
            amt_cls = "amt"
            summary_detail = f'{html.escape(from_account or "?")} → {html.escape(to_account or "?")}'
        else:
            if type_ == "refund":
                # Neither an expense nor income (§18) — same neutral tint as
                # a transfer, not the income green it was wrongly wearing.
                sign, amt_cls = "", "amt"
            else:
                sign = "−" if type_ == "expense" else "+"
                # The one accent: income reads as positive at a glance. The
                # sign carries the same meaning, so colour is never the sole
                # signal (WCAG 1.4.1).
                amt_cls = "amt" if type_ == "expense" else "amt in"
            summary_detail = html.escape(category) if _category_known(type_, category) else "Uncategorised"
        amt_suffix = ""
        if type_ == "expense" and refunded_so_far > 0:
            amt_suffix = (
                " · refunded" if refunded_so_far >= amount
                else f" · {format_amount(refunded_so_far)} refunded"
            )
        if type_ == "refund":
            if refund_of_occurred_on is None:
                # The expense this refunds was soft-deleted after the fact —
                # both joined columns come back NULL together (§6 bypass
                # stays closed), so there is no date to show either.
                detail = "Refund of a deleted expense"
            else:
                name = f'"{html.escape(refund_of_note)}"' if refund_of_note else "an expense"
                detail = f"Refund of {name} · {refund_of_occurred_on:%d %b}"
            note_html = f'<div class="txn-note">{detail}</div>'
        else:
            note_html = f'<div class="txn-note">{html.escape(note)}</div>' if note else ""
        label = f"{sign}{format_amount(amount)} on {occurred_on:%d %b}"
        items.append(
            '<div class="txn">'
            f'<button type="button" class="txn-head" data-id="{txn_id}" '
            f'aria-expanded="false" aria-label="Edit {label}">'
            '<span class="txn-main">'
            f'<span>{occurred_on:%d %b} · {summary_detail}</span>'
            f'<span class="{amt_cls}">{sign}{format_amount(amount)}{amt_suffix}</span>'
            "</span>"
            + note_html
            + "</button>"
            + _txn_panel(
                txn_id, type_, amount, category, note, occurred_on, account_id, accounts, sign,
                amount - refunded_so_far,
            )
            + "</div>"
        )
    return '<section class="recent"><h2>Recent</h2>' + "".join(items) + "</section>"
