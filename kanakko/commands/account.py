"""`/account` — set an account's opening balance, or query a `locked` account's
contributions and payouts (§18).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 2/6).
`_command_arg`/`TextMessage` stay in `handlers.py` (the shared spine every
command module needs); importing them here is cheaper than a second copy.
"""

import time

import psycopg

from kanakko.db import get_or_create_user, locked_account_totals, set_account_opening_balance
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import TextMessage, _command_arg
from kanakko.money import format_amount, parse_amount
from kanakko.tg import send_message

ACCOUNT_COMMAND = "/account"

ACCOUNT_USAGE = (
    "Set an account's balance — e.g. /account bank 52000 or /account cash "
    "2000 (what's in it), /account credit 5000 (what you currently owe) or "
    "/account locked 20000 (what's already in an FD, SIP or chit). A new name "
    "creates that account."
)

ACCOUNT_BAD_AMOUNT = "That doesn't look like an amount — try /account credit 5000."

ACCOUNT_NO_HOUSEHOLD = "Send /start first — I need your household set up before I can add an account."

ACCOUNT_NOT_LOCKED = 'I don\'t have a locked account named "{name}" — set one up with /account locked <amount>.'


def _is_account(text: str) -> bool:
    """True when `text` is the `/account` command — bare or `/account@bot`."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == ACCOUNT_COMMAND


def _looks_like_amount(text: str) -> bool:
    """True when `text` parses as a ₹ amount — used to tell a botched onboarding
    kind ("cash 500") apart from a genuine multi-word `locked` account name
    ("Kids Fund") in `handle_account`."""
    try:
        parse_amount(text)
    except (TypeError, ValueError):
        return False
    return True


def _find_locked_account(conn: psycopg.Connection, user_id: int, name: str) -> dict | None:
    """Case-insensitive lookup of a live `locked` account by its full name —
    the parse layer's account names are the user's own nouns, so matching
    should not demand exact case."""
    return next(
        (t for t in locked_account_totals(conn, user_id) if t["name"].lower() == name.lower()),
        None,
    )


def _handle_account_query(
    conn: psycopg.Connection,
    msg: TextMessage,
    user_id: int,
    name: str,
    start: float,
    match: dict | None,
) -> dict | None:
    """`/account <name>` — the gross in/out a `locked` account has seen (§18).

    Split off from the two-argument onboarding form: text after `/account` that
    doesn't open with `credit`/`locked` is read as a query for that account's
    name (possibly several words — an auto-created pool's name is free text, see
    `new_locked_account`). `match` is the caller's lookup via
    `_find_locked_account` — `handle_account` needs that same lookup to decide
    whether a two-word arg is a query or a botched onboarding kind, so it is
    passed in rather than repeated here. Reports `locked_account_totals`' two
    ledger sums, never a balance (§18: "never carries a market value"), plus the
    opening balance the account was onboarded with when it is nonzero —
    otherwise a pool set up with `/account locked 20000` and never touched since
    reports "put in ₹0.00", which reads as if the starting balance were lost. A
    name that doesn't match a live `locked` account — wrong spelling, or a
    `spending`/`credit` account, which have no contribution/maturity story — is
    refused the same way.
    """
    if match is None:
        send_message(msg.chat_id, ACCOUNT_NOT_LOCKED.format(name=name))
        log_event("account.query", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    started = (
        f", started with {format_amount(match['opening_balance'])}"
        if match["opening_balance"] else ""
    )
    send_message(
        msg.chat_id,
        f"{match['name']} — put in {format_amount(match['contributed'])}, "
        f"got back {format_amount(match['paid_out'])}{started}.",
    )
    log_event("account.query", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              account_id=match["account_id"])
    return match


def handle_account(conn: psycopg.Connection, msg: TextMessage) -> dict | None:
    """Set an account's opening balance, or query a `locked` account's
    contributions and payouts (§18).

    `/account credit <amount>` and `/account locked <amount>` are the fixed
    onboarding asks `WELCOME` points to: `amount` is always what the user reports
    positively — "how much you owe" for a card, "how much is already in it" for
    savings or a spending account — `set_account_opening_balance` is the one place
    that turns it into the signed `opening_balance` the ledger stores (§18: "same
    column, different question"). Everyday spending already has a default account
    minted at household creation (`create_household_of_one`), so
    `/account <name> <amount>` — any first word that isn't `credit`/`locked` and
    isn't a live `locked` account's name — sets *that* spending account's opening
    balance, creating it on first use ("`/account cash 2000`"). `set_account_
    opening_balance` keys its lookup on the name (and kind) so this can never
    collide with a differently-named account of the same kind — see its docstring.
    Running any of these twice corrects a typo rather than minting a duplicate
    account. `/account <name>` — everything else, one word or several, with no
    amount — is the read side, delegated to `_handle_account_query`; a multi-word
    `locked` account name (an auto-created pool, see `new_locked_account`) is still
    reachable as a query because the lookup runs *before* the spending-account
    branch: a two-word arg whose second word parses as an amount ("Goa 2026") is
    read as a spending-account set only when no live `locked` account is named
    exactly that — otherwise an auto-created pool with a year or amount in its name
    would be misrouted away from its own totals, the exact regression a prior
    review (F4 against `aaaeb21`) named.
    Does not commit — the caller owns the transaction. Returns the account row
    on success, or `None` on a usage/validation/query refusal — including a
    sender with no household yet (open signup mode, before their first
    `/start`).
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    arg = _command_arg(msg.text)
    parts = arg.split(maxsplit=1) if arg else []

    if not parts:
        send_message(msg.chat_id, ACCOUNT_USAGE)
        log_event("account.set_up", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    first = parts[0].lower()
    if first in {"credit", "locked"}:
        if len(parts) == 1:
            send_message(msg.chat_id, ACCOUNT_USAGE)
            log_event("account.set_up", status="noop", update_id=msg.update_id,
                      source=msg.source, user_id=user_id, duration_ms=ms_since(start))
            return None
        kind, name = first, None
    else:
        match = _find_locked_account(conn, user_id, arg)
        if match is not None:
            return _handle_account_query(conn, msg, user_id, arg, start, match)
        if len(parts) != 2 or not _looks_like_amount(parts[1]):
            return _handle_account_query(conn, msg, user_id, arg, start, match)
        kind, name = "spending", parts[0]

    amount_text = parts[1]
    try:
        amount = parse_amount(amount_text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, ACCOUNT_BAD_AMOUNT)
        log_event("account.set_up", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    account = set_account_opening_balance(conn, user_id, kind, amount, name=name)
    if account is None:
        send_message(msg.chat_id, ACCOUNT_NO_HOUSEHOLD)
        log_event("account.set_up", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    verb = "owe" if kind == "credit" else "have"
    send_message(
        msg.chat_id,
        f"Got it — {account['name']} ({kind}), you {verb} {format_amount(amount)}.",
    )
    log_event("account.set_up", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              account_id=account["account_id"], kind=kind, amount=amount)
    return account
