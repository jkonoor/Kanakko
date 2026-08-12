"""Server-rendered dashboard fragment — figures and bars (§13).

Python and CSS only, no charting library (§13). The recent-transactions list —
per-row delete, category change, and field edit — is `kanakko.webapp.recent`;
this module is the period panels `dashboard_html` wraps around it.
"""

import html
from decimal import Decimal

from kanakko.money import format_amount
from kanakko.webapp.periods import Period
from kanakko.webapp.recent import recent_list


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


def recurring_list(rules: list[dict]) -> str:
    """The recurring-rules list: pause/resume and delete per rule (§13, §16, §18).

    Each `rules` entry is a dict from `db.list_recurring_rules` —
    `{rule_id, account_id, account_name, category, amount, day_of_month,
    active}`. A rule reads as "Food · ₹500 · day 5 · Bank" so it names every
    field the cron will act on, not just the amount. `data-active` on the
    toggle carries the rule's *current* state, since the toggle POSTs the
    state to move *to* — `shell.py`'s click handler reads it and sends the
    opposite, mirroring `_account_select`'s `data-id` convention. A paused
    rule gets a `paused` class (dimmed in CSS) rather than being hidden — you
    must still see a rule to resume it. Household-scoped like every other
    figure on this dashboard (§16): `list_recurring_rules` already restricts
    to the caller's household, so nothing further to check here. An empty
    list — no rules yet, since creation has no surface yet either — renders
    nothing, same as an empty `recent_list`.
    """
    if not rules:
        return ""
    items = []
    for r in rules:
        cls = "rule paused" if not r["active"] else "rule"
        toggle_glyph = "▶" if not r["active"] else "⏸"
        toggle_label = "Resume" if not r["active"] else "Pause"
        items.append(
            f'<div class="{cls}">'
            f'<div class="rule-main"><span>{html.escape(r["category"])} · '
            f'{format_amount(r["amount"])} · day {r["day_of_month"]} · '
            f'{html.escape(r["account_name"])}</span></div>'
            f'<button type="button" class="rule-toggle" data-id="{r["rule_id"]}" '
            f'data-active="{"true" if r["active"] else "false"}" '
            f'aria-label="{toggle_label} {html.escape(r["category"])} recurring rule">'
            f'{toggle_glyph}</button>'
            f'<button type="button" class="rule-del" data-id="{r["rule_id"]}" '
            f'aria-label="Delete {html.escape(r["category"])} recurring rule">✕</button>'
            "</div>"
        )
    return '<section class="recurring"><h2>Recurring</h2>' + "".join(items) + "</section>"


def dashboard_html(
    periods: list[Period],
    recent: list[tuple],
    selected: str = "month",
    accounts: list[tuple[int, str]] = (),
    rules: list[dict] = (),
) -> str:
    """The dashboard fragment: a period switcher, one panel per period, the list (§13).

    Net is `income - expenses` — exact `Decimal` subtraction, can be negative.
    It is named "Net", not "Balance": §18 gave "Balance" a real, different meaning
    (an account's stock, `opening_balance + inflows − outflows`) and this figure
    is the period's flow, not a pool's contents. Server-rendered so every section
    stays Python + CSS with no charting library (§13), and `recent` is the
    recent-transactions list with per-row delete and edit; `accounts` is the
    household's live accounts (§18) the row editor's account `<select>` offers.
    `rules` is the household's recurring rules (§18) — `recurring_list`'s pause/
    resume/delete section, rendered above the recent list since a standing
    instruction changes less often than a logged transaction but still belongs
    on the one screen. Formatted amounts and escaped labels / category names /
    notes are the only things interpolated — the note is the only user-typed
    string and `recent_list` escapes it, so nothing reaches the markup unescaped.

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
        + recurring_list(rules)
        + recent_list(recent, accounts)
    )
