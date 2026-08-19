"""`refund <amount>` — get money back on something you spent (§18).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 6/6).
`_command_arg`/`TextMessage`/`ButtonPress` stay in `handlers.py` (the shared
spine every command module needs); importing them here is cheaper than a
second copy.
"""

import time
from datetime import date
from decimal import Decimal

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.db import create_refund, get_or_create_user, refund_candidates
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import ButtonPress, TextMessage, _command_arg
from kanakko.money import format_amount, parse_amount
from kanakko.parse import today
from kanakko.tg import answer_callback_query, edit_message_text, send_message

REFUND_WORD = "refund"
REFUND_PREFIX = "rf:"

REFUND_USAGE = 'Say how much to refund — e.g. /refund 500.'
REFUND_BAD_AMOUNT = 'That doesn\'t look like an amount — try "refund 500".'
REFUND_NO_CANDIDATES = "I can't find a live expense that could take a refund like that."
REFUND_GONE = "That expense is gone — nothing to refund."
REFUND_OVER_LIMIT = "That's more than's left to refund on that expense."


def _is_refund(text: str) -> bool:
    """True for `/refund 500` or the bare `refund 500` — either opener.

    `/refund` is the canonical form: every other action here is a slash command,
    it is the only spelling BotFather can register, and a manual that has to
    explain "no leading /" is describing an inconsistency rather than removing
    one. The bare word keeps working because it shipped that way and reads
    naturally; `_command_arg` splits on whitespace, so both forms yield the same
    amount. Like `/undo`/`/remove` this needs its predicate in `app.py`'s
    `is_parse` exclusion list, or the LLM sees it and the daily cap counts it."""
    words = text.split()
    if not words:
        return False
    return words[0].split("@", 1)[0].lower() in (REFUND_WORD, f"/{REFUND_WORD}")


def _refund_keyboard(candidates: list[dict], amount: Decimal) -> InlineKeyboardMarkup:
    """One button per candidate, the reusable shape `categories.keyboard` and
    `confirm.account_keyboard` already use: N buttons from a list, one callback
    prefix, the tap routes on the value straight from `callback_data` — no label
    indirection needed, since `txn_id` is already the unique key. `amount` (the
    refund the user typed) rides along in the callback data because the tap is
    the only later signal `handle_refund_choice` gets; one button per row, unlike
    those two-per-row keyboards, because a candidate's label — amount, category,
    date — is longer than a category or account name.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{format_amount(c['amount'])} · {c['category']} · {c['occurred_on'].isoformat()}",
            callback_data=f"{REFUND_PREFIX}{c['txn_id']}:{amount}",
        )]
        for c in candidates
    ])


def handle_refund(conn: psycopg.Connection, msg: TextMessage) -> list[dict] | None:
    """`refund <amount>` — list live, not-fully-refunded expenses to refund `amount`
    against, amount-matched first (§18).

    Linking is by choosing, not by parsing (§18): the amount is the only thing
    this message commits to, and the button tap (`handle_refund_choice`) commits
    the rest. A bad or missing amount is refused with a usage hint, matching
    `handle_account`'s `ACCOUNT_BAD_AMOUNT`/`ACCOUNT_USAGE` split. No live
    candidate — a fresh household, or every expense already fully refunded —
    is refused too, rather than showing an empty chooser. Does not commit — the
    caller owns the transaction. Returns the candidate list shown, or `None` on
    a refusal.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    amount_text = _command_arg(msg.text)
    try:
        amount = parse_amount(amount_text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, REFUND_BAD_AMOUNT if amount_text else REFUND_USAGE)
        log_event("refund.listed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    candidates = refund_candidates(conn, user_id, amount)
    if not candidates:
        send_message(msg.chat_id, REFUND_NO_CANDIDATES)
        log_event("refund.listed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start),
                  amount=amount)
        return None

    send_message(msg.chat_id, f"Refund {format_amount(amount)} against which expense?",
                 reply_markup=_refund_keyboard(candidates, amount))
    log_event("refund.listed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              amount=amount)
    return candidates


def handle_refund_choice(conn: psycopg.Connection, press: ButtonPress) -> dict | None:
    """Act on an `rf:<txn_id>:<amount>` tap `handle_refund`'s chooser offered (§18).

    The button is untrusted the same way `handle_remove_choice`'s is: `create_refund`
    re-runs the full §16 household scope keyed by the *presser*, not the button,
    and only a live `expense` row is refundable — a stale or forged `txn_id` gets
    `None` back rather than a write. Migration 014's trigger is the guard that
    actually stops a refund from exceeding what's left (§18); `RaiseException` is
    caught here and turned into a plain reply rather than a 500 that would make
    Telegram redeliver the same tap forever — the race window this closes is a
    second refund landing between `handle_refund` listing the candidate and this
    tap being pressed. Does not commit — the caller owns the transaction. Returns
    the stored refund row, or `None` when refused.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    txn_id_str, _, amount_str = press.data.removeprefix(REFUND_PREFIX).partition(":")
    try:
        txn_id = int(txn_id_str)
        amount = parse_amount(amount_str)
    except (TypeError, ValueError):
        answer_callback_query(press.callback_query_id, "That button's expired")
        log_event("refund.created", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    try:
        refunded = create_refund(conn, user_id, txn_id, amount,
                                 date.fromisoformat(today()),
                                 source=press.source, update_id=press.update_id)
    except psycopg.errors.RaiseException:
        answer_callback_query(press.callback_query_id, REFUND_OVER_LIMIT)
        log_event("refund.created", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start),
                  outcome="over_limit")
        return None
    if refunded is None:
        answer_callback_query(press.callback_query_id, REFUND_GONE)
        log_event("refund.created", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    edit_message_text(
        press.chat_id, press.message_id,
        f"✅ Refunded {format_amount(amount)} — {refunded['category']}",
    )
    answer_callback_query(press.callback_query_id, "Refunded")
    log_event("refund.created", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start),
              txn_id=refunded["txn_id"], amount=amount)
    return refunded
