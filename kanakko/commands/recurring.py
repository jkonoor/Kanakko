"""`/recurring` — set up a recurring rule for an auto-debit (§18).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 5/6).
`_command_arg`/`TextMessage` stay in `handlers.py` (the shared spine every
command module needs); importing them here is cheaper than a second copy.
"""

import time

import psycopg

from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.db import create_recurring_rule, get_or_create_user, household_accounts
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import TextMessage, _command_arg
from kanakko.money import format_amount, parse_amount
from kanakko.tg import send_message

RECURRING_COMMAND = "/recurring"

RECURRING_USAGE = (
    "Set up an auto-debit — e.g. `/recurring 5000 5 Bills & Utilities Bank` "
    "(amount, day of month, category, account)."
)
RECURRING_BAD_AMOUNT = (
    "That doesn't look like an amount — try "
    "`/recurring 5000 5 Bills & Utilities Bank`."
)
RECURRING_BAD_DAY = (
    "Day of month should be 1-31 — try `/recurring 5000 5 Bills & Utilities Bank`."
)
RECURRING_BAD_CATEGORY_ACCOUNT = (
    'I couldn\'t find a category and account in "{rest}" — category has to be '
    "one of {categories}, and account one of yours."
)
RECURRING_NO_HOUSEHOLD = (
    "Send /start first — I need your household set up before I can add a recurring rule."
)


def _is_recurring(text: str) -> bool:
    """True when `text` is the `/recurring` command — bare or `/recurring@bot`."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == RECURRING_COMMAND


def _match_category_and_account(
    accounts: list[tuple[int, str]], rest: str
) -> tuple[str, int, str] | None:
    """Split `rest` into a known expense category and one of `accounts` (§18).

    Both are closed sets, but there is no delimiter between them in the typed
    command, and either can be more than one word ("Bills & Utilities", "Kids
    Fund"), so this checks every category as a candidate prefix rather than
    splitting on a fixed position. Auto-debits are always an expense
    (`jobs.recurring` hardcodes `type="expense"`), so the category side is
    `EXPENSE_CATEGORIES`, not §11's full category union.
    """
    by_name = {name.lower(): (account_id, name) for account_id, name in accounts}
    rest_lower = rest.lower()
    for category in EXPENSE_CATEGORIES:
        prefix = f"{category.lower()} "
        if not rest_lower.startswith(prefix):
            continue
        match = by_name.get(rest[len(prefix):].strip().lower())
        if match is not None:
            account_id, account_name = match
            return category, account_id, account_name
    return None


def handle_recurring(conn: psycopg.Connection, msg: TextMessage) -> dict | None:
    """`/recurring <amount> <day> <category> <account>` — set up a recurring
    rule for an auto-debit (§18).

    The `/account`-style shape: `amount` and `day` are unambiguous (a number,
    then 1-31), so they anchor the front of the command; everything after them
    is `category`+`account` text with no delimiter between the two, resolved by
    `_match_category_and_account` against the caller's own closed sets — the
    account list comes from `household_accounts`, never a literal (§18). The
    rule always starts active; pausing or deleting it is the dashboard's job
    (`kanakko.webapp.recurring`), not this command's. Does not commit — the
    caller owns the transaction. Returns the stored rule on success, or `None`
    on a usage/validation refusal — including a sender with no household yet
    (open signup mode, before their first `/start`).
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    arg = _command_arg(msg.text)
    parts = arg.split(maxsplit=2) if arg else []

    if len(parts) < 3:
        send_message(msg.chat_id, RECURRING_USAGE)
        log_event("recurring.created", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    amount_text, day_text, rest = parts
    try:
        amount = parse_amount(amount_text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, RECURRING_BAD_AMOUNT)
        log_event("recurring.created", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    if not day_text.isdigit() or not (1 <= int(day_text) <= 31):
        send_message(msg.chat_id, RECURRING_BAD_DAY)
        log_event("recurring.created", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    day_of_month = int(day_text)

    accounts = household_accounts(conn, user_id)
    if not accounts:
        send_message(msg.chat_id, RECURRING_NO_HOUSEHOLD)
        log_event("recurring.created", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None

    match = _match_category_and_account(accounts, rest)
    if match is None:
        send_message(
            msg.chat_id,
            RECURRING_BAD_CATEGORY_ACCOUNT.format(
                rest=rest, categories=", ".join(EXPENSE_CATEGORIES)
            ),
        )
        log_event("recurring.created", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    category, account_id, account_name = match

    rule = create_recurring_rule(conn, user_id, account_id, category, amount, day_of_month)
    send_message(
        msg.chat_id,
        f"Got it — {format_amount(amount)} for {category} from {account_name} on "
        f"day {day_of_month} of the month. Manage it from Dashboard.",
    )
    log_event("recurring.created", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              rule_id=rule["rule_id"], amount=amount, category=category,
              account_id=account_id, day_of_month=day_of_month)
    return rule
