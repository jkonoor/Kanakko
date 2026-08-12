"""Server-rendered dashboard fragment — figures, bars, and the recent list (§13).

Python and CSS only, no charting library (§13). The note is the one user-typed
string that reaches the markup and is HTML-escaped here; nothing else is.
"""

import html
from decimal import Decimal

from kanakko.categories import CATEGORIES_BY_TYPE
from kanakko.money import format_amount
from kanakko.webapp.periods import Period


def _delta(current: Decimal, previous: Decimal | None, label: str) -> str:
    """The hero's period-over-period change: `▼ 40% vs last month`, or nothing.

    A figure with no baseline is a record, not an insight, so each hero carries
    how it compares to the same-length period before it. Direction rides the
    arrow glyph *and* the label text, never colour alone (WCAG 1.4.1); the whole
    line stays in hint ink, so expenses are not tinted (§13). A zero or absent
    baseline has no defined percentage — division by zero is *not* "100%" — so a
    first-ever period renders "no comparison yet" rather than a fabricated number.
    `previous` is `None` for a range with no prior period (all-time). Arithmetic
    is `Decimal` throughout; no float touches an amount (§9).
    """
    if previous is None:
        return ""
    if previous <= 0:
        return '<p class="delta">no comparison yet</p>'
    pct = round((current - previous) / previous * 100)  # Decimal — never float
    if pct == 0:
        return f'<p class="delta">about the same {html.escape(label)}</p>'
    arrow = "▲" if pct > 0 else "▼"
    return f'<p class="delta">{arrow} {abs(pct)}% {html.escape(label)}</p>'


def _stat(label: str, amount: Decimal, positive: bool = False) -> str:
    """One label/value pair. `amount` goes through `format_amount`, never float (§9).

    `positive` tints the value with the single accent this design allows (income).
    The accent is never the only signal — the label still says "Income" — per
    WCAG 1.4.1, which requires colour not be the sole carrier of meaning.
    """
    cls = "value in" if positive else "value"
    return (
        f'<div class="stat"><span class="label">{label}</span>'
        f'<span class="{cls}">{format_amount(amount)}</span></div>'
    )


def _period_panel(period: Period, selected: bool) -> str:
    """One period's panel: a hero spend figure, two supporting stats, its bars.

    The hero is *expenses*, not balance — "what have I spent?" is the question
    this screen gets opened for, and giving one figure dominant scale is what
    gives the screen an entry point (NN/g: importance is shown by size). Net
    and income stay as small supporting stats rather than competing at equal
    weight, which is what the previous layout did.

    All panels are rendered server-side and switched client-side, so changing
    period costs no request and no reload.
    """
    hidden = "" if selected else " hidden"
    return (
        f'<section class="panel" data-period="{period.key}"{hidden}>'
        f'<p class="hero-label">Spent · {html.escape(period.label)}</p>'
        f'<p class="hero">{format_amount(period.expenses)}</p>'
        + _delta(period.expenses, period.prev_expenses, period.compare_label)
        + '<div class="substats">'
        + _stat("Net", period.income - period.expenses)
        + _stat("Income", period.income, positive=True)
        + "</div>"
        + category_bars(period.top, period.expenses)
        + "</section>"
    )


def category_bars(categories: list[tuple[str, Decimal]], total: Decimal) -> str:
    """This month's expense categories as a sorted list with CSS percentage bars (§13).

    `categories` is `(name, amount)` biggest-first (straight from
    `db.month_summary`'s `top`); `total` is the month's expenses, the denominator
    each bar's width is a share of. No charting library — the bar is a `<div>`
    whose inline `width` is the category's percentage of `total`, exact `Decimal`
    arithmetic so no float touches an amount (§9). Category names are HTML-escaped:
    they come from the fixed `categories.py` set today, but escaping keeps the
    fragment safe if a user-named category ever reaches it. An empty month, or a
    `total` of 0, yields no section.
    """
    if not categories or total <= 0:
        return ""
    rows = []
    for name, amount in categories:
        pct = amount / total * 100  # Decimal / Decimal — never float
        # The share is the point of a breakdown — "Food ₹200" doesn't say Food is
        # two thirds of the spend, and the bar length alone is only comparable
        # against its neighbours. The number makes it readable on its own.
        rows.append(
            '<div class="cat">'
            f'<div class="cat-head"><span>{html.escape(name)}</span>'
            f'<span class="cat-amt">{format_amount(amount)}'
            f'<span class="pct">{pct:.0f}%</span></span></div>'
            f'<div class="bar"><div class="fill" style="width:{pct:.1f}%"></div></div>'
            "</div>"
        )
    return '<h2>Spending by category</h2>' + "".join(rows)


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


def recent_list(rows: list[tuple]) -> str:
    """The recent-transactions list with per-row delete + category change (§13, §18, tasks 100–101).

    Each `rows` entry is `(txn_id, amount, type, category, note, occurred_on,
    from_account, to_account)` from `db.recent_transactions` — `from_account`/
    `to_account` are `NULL` for every type but `transfer` (§18). `note` is the
    first *user-typed* string the dashboard renders — §11 keeps the note's
    original wording, so it is arbitrary text that arrived through the bot — and
    it is HTML-escaped: an unescaped `<img src=x onerror=...>` in a logged expense
    would be stored XSS. A `transfer` has no category (it is excluded from both
    totals, §18) so it renders its two account names — "Bank → SIP" — instead of
    the category `<select>`, and carries neither the −/+ sign nor the income tint,
    since it is neither spending nor income. Every other row keeps the existing
    category `<select>` (a null category is "Uncategorised") that POSTs to
    `/app/category`, and its −/+ sign by direction. Amounts go through
    `format_amount` (§9). The delete button carries the `txn_id` for
    `POST /app/delete`. An empty ledger renders nothing.
    """
    if not rows:
        return ""
    items = []
    for txn_id, amount, type_, category, note, occurred_on, from_account, to_account in rows:
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
            "</div>"
        )
    return '<section class="recent"><h2>Recent</h2>' + "".join(items) + "</section>"


def dashboard_html(
    periods: list[Period], recent: list[tuple], selected: str = "month"
) -> str:
    """The dashboard fragment: a period switcher, one panel per period, the list (§13).

    Net is `income - expenses` — exact `Decimal` subtraction, can be negative.
    It is named "Net", not "Balance": §18 gave "Balance" a real, different meaning
    (an account's stock, `opening_balance + inflows − outflows`) and this figure
    is the period's flow, not a pool's contents. Server-rendered so every section
    stays Python + CSS with no charting library (§13), and `recent` is the
    recent-transactions list with per-row delete. Formatted amounts and escaped
    labels / category names / notes are the only things interpolated — the note
    is the only user-typed string and `recent_list` escapes it, so nothing
    reaches the markup unescaped.

    Why a switcher rather than three stacked sections: the old layout rendered
    Net/Income/Expenses three times over, nine near-identical rows for three
    concepts, and on a young ledger all three showed *the same numbers*. One
    panel at a time with the others one tap away is progressive disclosure —
    nothing is lost and the screen gets an entry point. There is no `<h1>` either;
    Telegram already shows the app's name in its own header directly above.

    Every panel ships in the fragment and switching is client-side, so the tabs
    cost no request. `selected` names the panel shown first.
    """
    tabs = "".join(
        f'<button type="button" class="seg" role="tab" data-period="{p.key}" '
        f'aria-selected="{"true" if p.key == selected else "false"}">'
        f'{html.escape(p.short)}</button>'
        for p in periods
    )
    panels = "".join(_period_panel(p, p.key == selected) for p in periods)
    return (
        f'<div class="switch" role="tablist" aria-label="Time period">{tabs}</div>'
        + panels
        + recent_list(recent)
    )
