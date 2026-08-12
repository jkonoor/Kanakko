"""What to do with a Telegram update — the model, the dispatch, the handlers.

Split out of `app.py`, which had grown to hold three jobs at once: the HTTP
surface, this update layer, and the Mini App routes. `app.py` keeps the routes
and owns the transaction; everything here is transport-agnostic and takes an open
connection, which is what makes it testable against Postgres without a webhook.

Every handler leaves the commit to its caller (§14), and none of them stores
anything on a path that failed.
"""

import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import psycopg
from pydantic import ValidationError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.auth import is_admin, signup_mode
from kanakko.categories import ALL_CATEGORIES, CATEGORY_PREFIX, EXPENSE_CATEGORIES
from kanakko.confirm import ACCOUNT_PREFIX, SKIP_LABEL, category_prompt, confirm_card, settled_card
from kanakko.db import (
    cancel_pending,
    check_removal,
    confirm_pending,
    consume_invite,
    create_household_invite,
    create_household_of_one,
    create_recurring_rule,
    create_refund,
    create_signup_invite,
    get_or_create_user,
    household_accounts,
    household_roster,
    list_accounts,
    refund_candidates,
    remove_member,
    request_amount_change,
    save_pending,
    set_pending_account,
    set_pending_amount,
    set_pending_category,
    undo_last,
    user_exists,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.money import format_amount, parse_amount
from kanakko.parse import Transaction, build_request, parse_message, resolve_model, today
from kanakko.tg import (
    answer_callback_query,
    delete_message,
    edit_message_text,
    get_bot_username,
    send_message,
)
from kanakko.trace import open_trace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextMessage:
    """A user typed something — a transaction to parse (§2).

    `from_id` is *who sent it* (Telegram `message.from.id`), the identity every
    handler resolves the user from (§16); `chat_id` is only the send target.
    They coincide in a private chat, so the two split apart the moment a
    household or a group exists. `from_id` falls back to `chat_id` when unset —
    the private-chat truth — which keeps the many test sites that predate the
    split valid.

    `update_id`/`source` are the §17 correlation fields, set by `dispatch`: the
    `update_id` is what ties every event of one Telegram delivery together, and
    `source` is always `webhook` here. They default so the many test
    construction sites that predate §17 stay valid.
    """

    chat_id: int
    message_id: int
    text: str
    from_id: int | None = None
    update_id: int | None = None
    source: str = "webhook"

    def __post_init__(self) -> None:
        if self.from_id is None:
            object.__setattr__(self, "from_id", self.chat_id)


@dataclass(frozen=True)
class ButtonPress:
    """A user tapped an inline button — Confirm/Cancel/category (§4, §5).

    `from_id` is the sender's identity, `chat_id` the send target — see
    `TextMessage`. `update_id`/`source`: the §17 correlation fields.
    """

    chat_id: int
    message_id: int
    callback_query_id: str
    data: str
    from_id: int | None = None
    update_id: int | None = None
    source: str = "webhook"

    def __post_init__(self) -> None:
        if self.from_id is None:
            object.__setattr__(self, "from_id", self.chat_id)


def dispatch(update: dict) -> TextMessage | ButtonPress | None:
    """Classify a Telegram update into the one action the core loop acts on.

    A text message becomes a `TextMessage`; an inline-button tap becomes a
    `ButtonPress`. Everything else — edited messages, photos, channel posts,
    bots joining — returns `None` and is ignored. The handlers that consume
    these live in the following tasks (parse→confirm card, Confirm, Cancel,
    category buttons).
    """
    update_id = update.get("update_id")
    message = update.get("message") or {}
    if isinstance(message.get("text"), str):
        chat = message.get("chat") or {}
        return TextMessage(
            chat_id=chat.get("id"),
            message_id=message.get("message_id"),
            text=message["text"],
            from_id=(message.get("from") or {}).get("id"),
            update_id=update_id,
        )

    callback = update.get("callback_query") or {}
    if isinstance(callback.get("data"), str):
        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        return ButtonPress(
            chat_id=chat.get("id"),
            message_id=msg.get("message_id"),
            callback_query_id=callback.get("id"),
            data=callback["data"],
            from_id=(callback.get("from") or {}).get("id"),
            update_id=update_id,
        )
    return None


# A failed parse and a lost user want different answers, so these are two strings.
# Conflating them cost the correction its point: a user whose real expense failed
# to parse was told what the bot does, never that the *amount* was the problem.
REPHRASE_PROMPT = (
    "I couldn't find an amount in that. Try again with the amount — like "
    '"spent 500 on groceries" or "got 20000 salary". Send /help to see '
    "everything I can do."
)

# The manual. Every command the bot answers is listed here, because nothing else
# lists them: `/household` mentioned `/invite` only in its solo reply, and
# `/remove` and `/transfer` described themselves only in their own error paths —
# so two whole features were reachable only by already knowing they existed.
#
# **"Dashboard" is the menu button's label, set in BotFather** (see
# docs/DEPLOYMENT.md). Naming it exactly as it reads on screen means the user maps
# a word to a thing they can see, instead of decoding "the menu button". That
# coupling is invisible to the test suite, which is why the label is written down.
#
# Deliberately no `web_app` button here: help exists to *teach* the permanent way
# in, and a shortcut on a message nobody revisits teaches "type /help first".
HELP_TEXT = (
    "I track what you spend and earn.\n\n"
    'Just tell me: "spent 500 on groceries", "got 20000 salary" — I\'ll show a '
    "card, and one tap confirms it.\n\n"
    "/undo — remove your last entry\n"
    "/household — who's in your household\n"
    "/invite <name> — a single-use link to add someone (owner only)\n"
    "/remove <name> — remove a member, or /remove on its own to leave\n"
    "/transfer <name> — hand over ownership\n"
    "/account credit <amount> — add a credit card, what you currently owe\n"
    "/account locked <amount> — add an FD/SIP/chit, what's already in it\n"
    "/account <name> — what an FD/SIP/chit has received and paid out\n"
    "/recurring <amount> <day> <category> <account> — an auto-debit I'll ask "
    "you to confirm each month, e.g. `/recurring 5000 5 Bills & Utilities Bank`\n"
    "refund <amount> — get money back on something you spent, no leading /\n\n"
    "Tap Dashboard at the bottom-left of the chat to see where your money went."
)

PARSER_DOWN_PROMPT = (
    "I can't reach my parser right now — your message wasn't saved. "
    "Please try again shortly."
)

# §18: the "Change amount" reply pair — asked once the button is tapped, and
# again if the reply couldn't be read as an amount. Tapping Skip on the card
# is the way out if the user doesn't want to answer either.
CHANGE_AMOUNT_PROMPT = "Reply with the new amount — just the number, like 5000."
CHANGE_AMOUNT_RETRY_PROMPT = (
    "I couldn't read that as an amount. Reply with just the number, like 5000."
)

ACCESS_REFUSED = (
    "This bot is invite-only right now. Ask whoever told you about it for an "
    "invite link to get started."
)

CAP_REACHED = (
    "You've hit today's message limit — nothing was saved. Your entries so far "
    "are safe, and this resets at midnight (IST)."
)

START_COMMAND = "/start"

# A /start deep-link payload: A-Z a-z 0-9 _ -, up to 64 chars (§16, verified
# against core.telegram.org/bots/features). Anything else is a garbage payload.
_START_PAYLOAD_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

WELCOME = (
    "Welcome to Kanakko — your personal finance tracker.\n\n"
    'Just tell me what you spent or earned — like "spent 500 on groceries" or '
    '"got 20000 salary" — and I\'ll log it after a one-tap confirm. Everyday '
    "spending already has a default account, so this works right away.\n\n"
    "Got a credit card or an FD/SIP/chit? `/account credit 5000` (what you owe) "
    "or `/account locked 20000` (what's already in it) adds it — skip this if "
    "you don't, nothing else needs it.\n\n"
    "Tap Dashboard at the bottom-left of the chat to see where your money went, "
    "and send /help any time for everything I can do."
)

# Three distinct refusals so a user knows which problem they have (§16 onboarding
# check): a spent link, an expired link, and a payload that was never a link.
START_SPENT = "That invite link has already been used. Ask for a fresh one."
START_EXPIRED = "That invite link has expired. Ask whoever invited you for a new one."
START_BAD_CODE = "That invite link isn't valid."


def _is_start(text: str) -> bool:
    """True when `text` is the `/start` command — bare or `/start@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == START_COMMAND


def _command_arg(text: str) -> str:
    """The argument after a command word — a `/start` payload, a `/invite` label —
    or `""` when the command was sent bare."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def handle_start(conn: psycopg.Connection, msg: TextMessage) -> str:
    """Onboard a `/start`: consume an invite, or open a household of one (§16).

    The bot's entry point, and the one handler that runs *before* the
    authorization gate (`app.py`): consuming an invite is how an unknown user
    becomes known, so the gate cannot precede it. `/start <code>` consumes the
    invite — a signup code opens a household of one, a household code joins that
    household — and a spent, expired, or invalid code is refused distinctly,
    storing nothing (§16). A bare `/start` explains the bot: it opens a household
    of one in `open` mode, welcomes an already-known user, and (in `invite` mode)
    turns an unknown user toward an invite. The payload is verified — 64 chars,
    `A-Z a-z 0-9 _ -` (§16) — and anything else is refused as invalid before any
    lookup. Does not commit — the caller owns the transaction. Returns the outcome
    slug for the log line.
    """
    start = time.perf_counter()
    payload = _command_arg(msg.text)
    if payload:
        if _START_PAYLOAD_RE.match(payload):
            outcome = consume_invite(conn, payload, msg.from_id)
        else:
            outcome = "unknown"  # a garbage payload — never a valid code (§16)
        reply = {
            "ok": WELCOME,
            "spent": START_SPENT,
            "expired": START_EXPIRED,
            "unknown": START_BAD_CODE,
        }[outcome]
    elif signup_mode() == "open":
        create_household_of_one(conn, get_or_create_user(conn, msg.from_id))
        outcome, reply = "ok", WELCOME
    elif user_exists(conn, msg.from_id):
        outcome, reply = "ok", WELCOME  # a known user, already in a household
    else:
        outcome, reply = "refused", ACCESS_REFUSED  # invite mode, no invite

    send_message(msg.chat_id, reply)
    log_event("user.onboarded", status="ok" if outcome == "ok" else "noop",
              update_id=msg.update_id, source=msg.source,
              duration_ms=ms_since(start), outcome=outcome)
    return outcome


def handle_text(conn: psycopg.Connection, msg: TextMessage) -> int | None:
    """Parse a typed message and send its confirm card (§2, §4) — first half of
    the core loop, split off from Handle Confirm.

    Resolve the user, parse the text into a `Transaction`, send the confirm card,
    and store the parsed row keyed by the *sent card's* message id — the id the
    Confirm/Cancel tap carries back (§1). The card is sent *before* the pending
    row is written because that message id doesn't exist until Telegram assigns
    it; a send that fails leaves no pending row, which is the safe direction (a
    dead card the user can retry, never a Confirm with nothing to confirm).

    A message with no parseable amount is no transaction (§3): after `parse_message`
    exhausts its one retry the `ValidationError` propagates here, and instead of
    500ing (which makes Telegram redeliver the same unparseable text forever) we
    ask the user to rephrase and store nothing. Returns `None` in that case.

    A **4xx** from OpenRouter is permanent (§6): a bad key (401), exhausted credits
    (402), a rejected schema (400) — no amount of Telegram redelivery fixes it. We
    tell the user the parser is unreachable, store nothing, and return `None` so the
    webhook answers 200 and the retry loop stops. A **5xx** or network/timeout error
    is transient, so it propagates: the webhook 500s and Telegram's redelivery is the
    recovery (mirrors the Handle Confirm contract). Store nothing either way. Either
    upstream failure is logged at WARNING with the status and provider body so the
    next occurrence is diagnosable from the container log (the API key rides in the
    request headers, not the body, so it can't leak into the line).

    A null `category` means the model couldn't tell (§3): we show the category
    picker instead of a confirm card so the user names it in one tap. The pending
    row is still written (keyed by the sent card's id) so the category press can
    update it; the amount, not the category, is what makes it a transaction. A
    `transfer` (§18) always has a null category — it is neither spending nor
    income — so it skips the picker and goes straight to its own confirm card.

    The user is resolved from `from_id` (the sender), never `chat_id` (the send
    target) — they coincide in a private chat but split under a household or group
    (§16). Does not commit — the caller owns the transaction.
    Returns the new `pending_id`, or `None` when the message couldn't be parsed.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    # §18: the parse schema's account enum, built per request from the caller's
    # own household — never a literal, the same rule §11 applies to categories.
    accounts = list_accounts(conn, user_id)
    # Trace mode (§17): the raw text, the prompt built from it, and the outcome —
    # written to a per-update folder so a hard parse bug is diagnosable. On by
    # default, a no-op when disabled or unconfigured, and it never raises.
    tr = open_trace(msg.update_id)
    tr.write("input", {"text": msg.text, "user_id": user_id,
                       "update_id": msg.update_id, "source": msg.source})
    tr.write("request", build_request(msg.text, accounts=accounts))
    parse_start = time.perf_counter()
    try:
        txn = parse_message(msg.text, accounts)
    except ValidationError as exc:
        tr.write("parse", {"error": str(exc)}, outcome="invalid")
        send_message(msg.chat_id, REPHRASE_PROMPT)
        return None
    except httpx.HTTPStatusError as exc:
        # The API key rides in the request headers, not the response body, so
        # logging status + body can't leak it (asserted in the check).
        log.warning(
            "parse upstream failure: status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        tr.write("parse", {"status": exc.response.status_code,
                           "body": exc.response.text},
                 outcome=f"upstream_{exc.response.status_code}")
        if not 400 <= exc.response.status_code < 500:
            raise  # 5xx is transient — let it 500 so Telegram redelivers
        send_message(msg.chat_id, PARSER_DOWN_PROMPT)
        return None
    tr.write("parse", txn.model_dump(), outcome="ok")
    # §17: the success side of the parse — Phase 6's WARNING covers the failure.
    # `duration_ms` is the call alone (retry included) so a slow model is visible
    # before it reads as the bot feeling sluggish.
    log_event("parse.completed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id,
              duration_ms=ms_since(parse_start), model=resolve_model())
    if txn.type != "transfer" and txn.category is None:
        text, keyboard = category_prompt(txn)
    else:
        text, keyboard = confirm_card(txn, accounts)
    sent = send_message(msg.chat_id, text, reply_markup=keyboard)
    card_message_id = sent["result"]["message_id"]
    pending_id = save_pending(conn, user_id, card_message_id, txn)
    log_event("pending.created", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return pending_id


UNDO_COMMAND = "/undo"


def _is_undo(text: str) -> bool:
    """True when `text` is the `/undo` command — bare or `/undo@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == UNDO_COMMAND


def handle_undo(conn: psycopg.Connection, msg: TextMessage) -> dict | None:
    """Soft-delete the user's most recent confirmed transaction and confirm it (§5, §6).

    `/undo` is the correction path for a just-confirmed entry: `undo_last` sets
    `deleted_at` on the newest live row (recoverable, §6) and returns its fields
    so the reply names exactly what was removed. Nothing to undo — a fresh user,
    or a `/undo` past the last row — gets a plain "Nothing to undo." Does not
    commit — the caller owns the transaction. Returns the removed row, or `None`.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    removed = undo_last(conn, user_id, source=msg.source, update_id=msg.update_id)
    if removed is None:
        send_message(msg.chat_id, "Nothing to undo.")
        log_event("transaction.undone", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    line = f"Removed: {removed['type'].capitalize()} — {format_amount(removed['amount'])}"
    if removed["category"]:
        line += f" ({removed['category']})"
    send_message(msg.chat_id, line)
    log_event("transaction.undone", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              txn_id=removed["txn_id"], amount=removed["amount"])
    return removed


HELP_COMMAND = "/help"

# The greetings a lost user actually types, matched on an EXACT normalised equality
# — never a substring or heuristic. Any looser test ("no digits", "ends in ?") would
# eventually swallow a real expense: "spent five hundred on lunch" has no digits and
# "500 lunch" has no verb, and a silently refused entry costs the trust the ledger
# runs on. A stray LLM call costs a fraction of a rupee, so the parse path stays the
# default and only these exact strings short-circuit it. "spent 500 on hi" is not an
# exact match, so it can never misfire.
_GREETINGS = frozenset({
    "hi", "hello", "hey", "help", "thanks", "thank you",
    "what can you do", "how do i use this",
})


def _is_help(text: str) -> bool:
    """True when `text` is the `/help` command — bare or `/help@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == HELP_COMMAND


def _is_greeting(text: str) -> bool:
    """True when the normalised `text` (lowercased, edge whitespace and punctuation
    trimmed) is exactly a known greeting — the zero-LLM-cost short-circuit (§ chat
    polish). Exact match only, so a real entry can never be mistaken for one."""
    normalised = text.strip().lower().strip(string.punctuation + string.whitespace)
    return normalised in _GREETINGS


def handle_help(conn: psycopg.Connection, msg: TextMessage) -> None:
    """Answer a lost user with the full command list (§ chat polish), routed from
    `/help` and from an exact greeting, making zero OpenRouter calls.

    Sends `HELP_TEXT`, **not** the failed-parse correction: help is a manual and a
    correction is a nudge, and one string doing both left the correction unable to
    say what actually went wrong. Does not commit — the caller owns the transaction.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    send_message(msg.chat_id, HELP_TEXT)
    log_event("help.sent", status="ok", update_id=msg.update_id, source=msg.source,
              user_id=user_id, duration_ms=ms_since(start))


INVITE_COMMAND = "/invite"

INVITE_USAGE = (
    "Add a label so you can tell who's who — e.g. `/invite ravi`. Each invite is "
    "a single-use link to join your household."
)

INVITE_NOT_OWNER = (
    "Only the household owner can invite people. Ask whoever set up your household "
    "to send an invite."
)


def _is_invite(text: str) -> bool:
    """True when `text` is the `/invite` command — bare or `/invite@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == INVITE_COMMAND


def handle_invite(conn: psycopg.Connection, msg: TextMessage) -> str | None:
    """Issue a labelled single-use household invite link — owner only (§16).

    `/invite <label>` mints a `household` invite for the household this user owns
    and replies with its `https://t.me/<bot>?start=<code>` deep link; the label
    (`ravi`, `priya`) is how the operator tells who is active (§16). Owner-only is
    enforced in `create_household_invite`: a member who isn't the owner gets a
    refusal and no row. A bare `/invite` with no label is a usage hint, not a row.
    The code is a random base64url token (valid `/start` payload — A-Z a-z 0-9 _ -,
    §16), so a collision is astronomically unlikely; if one ever did occur the
    UNIQUE on `invites.code` raises, the update's claim rolls back, and Telegram's
    redelivery mints a fresh code. Does not commit — the caller owns the
    transaction. Returns the issued code, or `None` when nothing was issued.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    label = _command_arg(msg.text)
    if not label:
        send_message(msg.chat_id, INVITE_USAGE)
        log_event("invite.issued", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    code = "h-" + secrets.token_urlsafe(9)
    if not create_household_invite(conn, user_id, code, label):
        send_message(msg.chat_id, INVITE_NOT_OWNER)
        log_event("invite.issued", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    link = f"https://t.me/{get_bot_username()}?start={code}"
    send_message(msg.chat_id,
                 f"Invite for {label} — a single-use link to join your household:\n{link}")
    log_event("invite.issued", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return code


INVITE_SIGNUP_COMMAND = "/invite_signup"

INVITE_SIGNUP_USAGE = (
    "Add a label so you can tell who's who — e.g. `/invite_signup ravi`. Each link "
    "is single-use and gives that person their own household."
)

INVITE_SIGNUP_NOT_ADMIN = (
    "Only the operator can issue signup invites. `/invite <name>` adds someone to "
    "your own household."
)


def _is_invite_signup(text: str) -> bool:
    """True when `text` is the `/invite_signup` command — bare or `@bot`-suffixed.

    An underscore rather than the hyphen this reads as in prose: Telegram recognises
    only `a-z 0-9 _` in a command, so `/invite-signup` splits at the hyphen and
    BotFather cannot register it — it would work when typed by hand and be invisible
    everywhere else.
    """
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == INVITE_SIGNUP_COMMAND


def handle_invite_signup(conn: psycopg.Connection, msg: TextMessage) -> str | None:
    """Issue a labelled single-use signup invite link — operator only (§16).

    `/invite_signup <label>` is how a tester gets in: the link admits them to the bot
    and gives them a household of one, so their money is nobody else's business —
    unlike `/invite`, which adds them to the caller's household. Gated on
    `is_admin` (the environment, not the database — see `auth.is_admin`) because a
    signup invite hands out the bot itself; a non-admin gets a refusal and no row.
    A bare `/invite_signup` is a usage hint, not a row.

    Same code shape and collision reasoning as `handle_invite`, with an `s-` prefix
    so a glance at the `invites` table tells the two grants apart. Does not commit —
    the caller owns the transaction. Returns the issued code, or `None`.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    if not is_admin(msg.from_id):
        send_message(msg.chat_id, INVITE_SIGNUP_NOT_ADMIN)
        log_event("invite.signup_issued", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    label = _command_arg(msg.text)
    if not label:
        send_message(msg.chat_id, INVITE_SIGNUP_USAGE)
        log_event("invite.signup_issued", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    code = "s-" + secrets.token_urlsafe(9)
    create_signup_invite(conn, user_id, code, label)
    link = f"https://t.me/{get_bot_username()}?start={code}"
    send_message(msg.chat_id,
                 f"Signup invite for {label} — single-use, gives them their own "
                 f"household:\n{link}")
    log_event("invite.signup_issued", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return code


HOUSEHOLD_COMMAND = "/household"

HOUSEHOLD_SOLO = (
    "👥 Your household — just you so far. Use `/invite <name>` to add someone."
)


def _is_household(text: str) -> bool:
    """True when `text` is the `/household` command — bare or `/household@bot`."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == HOUSEHOLD_COMMAND


def handle_household(conn: psycopg.Connection, msg: TextMessage) -> str:
    """Show who is in the sender's household and who owns it (§16).

    A read, never a mutation: lists every member with their invite label, marks
    the owner and the viewer, and points a solo user at `/invite`. Member removal
    is a separate operation (§16 — it asks retain-or-delete of the departing
    member's entries). Does not commit. Returns the outcome slug for the log line.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    members = household_roster(conn, user_id)
    if len(members) <= 1:
        reply = HOUSEHOLD_SOLO
    else:
        lines = []
        for member_id, is_owner, label in members:
            name = "Owner" if is_owner else (label or "Member")
            if member_id == user_id:
                name += " (you)"
            lines.append(f"• {name}")
        # The footer is the only place `/remove` and `/transfer` are ever named:
        # both described themselves only in their own error paths, which you
        # cannot reach without already knowing the command. Here they appear at
        # the one moment they mean anything — looking at a household with someone
        # else in it. Owner-only commands are shown only to the owner, so a member
        # is never told to try something that will refuse them.
        viewer_owns = any(m == user_id and owns for m, owns, _ in members)
        footer = ["`/invite <name>` — add someone"] if viewer_owns else []
        footer.append(
            "`/remove <name>` — remove a member" if viewer_owns
            else "`/remove` — leave this household"
        )
        if viewer_owns:
            footer.append("`/transfer <name>` — hand over ownership")
        reply = (
            f"👥 Your household — {len(members)} members\n\n"
            + "\n".join(lines)
            + "\n\n"
            + "\n".join(footer)
        )
    send_message(msg.chat_id, reply)
    log_event("household.viewed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return "ok"


REMOVE_COMMAND = "/remove"

# Bare `/remove` leaves the household yourself; `/remove <label>` is the owner
# removing that member. So there is no "usage" error — the bare form is an action.
REMOVE_NO_MATCH = (
    "No member is labelled {label!r}. Check `/household` for the exact labels, or "
    "send `/remove` on its own to leave the household yourself."
)
REMOVE_AMBIGUOUS = (
    "More than one member is labelled {label!r}, so I won't guess which to remove. "
    "Give them distinct invite labels first (`/invite`)."
)
REMOVE_NOT_OWNER = (
    "Only the household owner can remove other members. To leave the household "
    "yourself, send `/remove` on its own."
)
REMOVE_OWNER_MUST_TRANSFER = (
    "You own this household, so you can't leave it — a household always needs an "
    "owner. Hand ownership to someone first with `/transfer <name>`, then you can "
    "leave."
)
REMOVE_NOT_MEMBER = "That person isn't in your household."

# The retain/delete choice a removal asks (§16). The button carries the target's
# user id — `remove_member` re-authorizes the presser against it, so the id is a
# routing hint, not a trust boundary. `rm:retain:<id>` / `rm:delete:<id>`.
REMOVE_PREFIX = "rm:"
REMOVE_RETAIN = "retain"
REMOVE_DELETE = "delete"

# §16 requires the warning to name the specific consequence — hard deletion makes
# past reports stop reconciling — not just "this cannot be undone".
_REMOVE_WARNING = (
    "{lead}\n\n"
    "Keep {whose} past entries in the shared ledger, or delete them for good?\n\n"
    "⚠️ Deleting is permanent — it can't be undone, and past reports stop matching: "
    "a month that summarised ₹18,920 won't reconcile when it's re-opened."
)


def _is_remove(text: str) -> bool:
    """True when `text` is the `/remove` command — bare or `/remove@bot` in a group."""
    words = text.split()
    return bool(words) and words[0].split("@", 1)[0].lower() == REMOVE_COMMAND


def _removal_keyboard(target_id: int) -> InlineKeyboardMarkup:
    """The Keep/Delete choice offered before a removal acts (§16)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Keep entries", callback_data=f"{REMOVE_PREFIX}{REMOVE_RETAIN}:{target_id}"),
        InlineKeyboardButton(
            "Delete entries", callback_data=f"{REMOVE_PREFIX}{REMOVE_DELETE}:{target_id}"),
    ]])


def handle_remove(conn: psycopg.Connection, msg: TextMessage) -> int | None:
    """Ask retain-or-delete before removing a household member (§16).

    `/remove <label>` targets the member carrying that invite label; a bare
    `/remove` targets the sender (the `actor == target` case §16 folds into one
    path). The label is resolved against the sender's own `/household` roster, so it
    can only name a member of their household; the owner has no label (they created
    the household, not joined by invite), so `/remove <label>` can never target the
    owner — an owner leaves only via the bare form, which is refused until ownership
    transfers (§16). A label matching no member, or two (labels aren't unique), is
    refused without asking.

    Authorization is checked here (`check_removal`) so the retain/delete warning is
    shown *only* when the removal will go through. The removal itself is deferred to
    the button tap (`handle_remove_choice`): §16 asks what to do with the departing
    member's entries first, warning that deletion makes past reports stop
    reconciling. Does not commit — the caller owns the transaction. Returns the
    target's `user_id` when the choice is presented, or `None` when refused.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    label = _command_arg(msg.text)
    if not label:
        target_id, is_self = user_id, True  # bare /remove — leave the household yourself
    else:
        matches = [
            member_id
            for member_id, _is_owner, member_label in household_roster(conn, user_id)
            if member_label and member_label.lower() == label.lower()
        ]
        if len(matches) != 1:
            reply = (REMOVE_NO_MATCH if not matches else REMOVE_AMBIGUOUS).format(label=label)
            send_message(msg.chat_id, reply)
            log_event("member.removed", status="noop", update_id=msg.update_id,
                      source=msg.source, user_id=user_id, duration_ms=ms_since(start))
            return None
        target_id, is_self = matches[0], False

    verdict = check_removal(conn, user_id, target_id)
    if verdict != "ok":
        send_message(msg.chat_id, {
            "not_owner": REMOVE_NOT_OWNER,
            "owner_must_transfer": REMOVE_OWNER_MUST_TRANSFER,
            "not_member": REMOVE_NOT_MEMBER,
        }[verdict])
        log_event("member.removed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start),
                  outcome=verdict)
        return None

    lead = "Leave the household?" if is_self else f"Remove {label} from your household?"
    whose = "your" if is_self else "their"
    send_message(msg.chat_id, _REMOVE_WARNING.format(lead=lead, whose=whose),
                 reply_markup=_removal_keyboard(target_id))
    log_event("member.removed", status="noop", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start),
              outcome="asked")
    return target_id


def handle_remove_choice(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Act on the retain/delete button a removal warning offered (§16).

    `handle_remove` presented the choice with the warning; this tap performs it.
    The button is untrusted — `remove_member` re-runs the full §16 authorization
    keyed by the *presser* (`from_id`), not the button — so a forged
    `rm:delete:<id>` for a member the presser can't remove is refused here exactly
    as the typed command would be. `rm:delete:<id>` hard-deletes the departing
    member's entries (irreversible, §16 — not §6's soft delete); `rm:retain:<id>`
    keeps them. The warning card is edited into a settled state so the transcript
    records the outcome. Does not commit — the caller owns the transaction. Returns
    the target's `user_id` on removal, or `None` when refused.
    """
    start = time.perf_counter()
    actor = get_or_create_user(conn, press.from_id)
    choice, _, target_str = press.data.removeprefix(REMOVE_PREFIX).partition(":")
    if choice not in (REMOVE_RETAIN, REMOVE_DELETE) or not target_str.isdigit():
        answer_callback_query(press.callback_query_id, "That button's expired")
        log_event("member.removed", status="noop", update_id=press.update_id,
                  source=press.source, user_id=actor, duration_ms=ms_since(start))
        return None
    target_id = int(target_str)
    delete_entries = choice == REMOVE_DELETE

    outcome = remove_member(conn, actor, target_id, delete_entries=delete_entries)
    if outcome != "removed":
        answer_callback_query(press.callback_query_id, {
            "not_owner": REMOVE_NOT_OWNER,
            "owner_must_transfer": REMOVE_OWNER_MUST_TRANSFER,
            "not_member": REMOVE_NOT_MEMBER,
        }[outcome])
        log_event("member.removed", status="noop", update_id=press.update_id,
                  source=press.source, user_id=actor, duration_ms=ms_since(start),
                  outcome=outcome)
        return None

    if target_id == actor:
        settled = "You've left the household. You're tracking on your own again now."
        settled += (" Your past entries were deleted." if delete_entries
                    else " Your past entries stay in the shared ledger.")
    else:
        settled = "Removed from your household."
        settled += (" Their past entries were deleted." if delete_entries
                    else " Their past entries stay in the shared ledger.")
    edit_message_text(press.chat_id, press.message_id, settled)
    answer_callback_query(press.callback_query_id, "Done")
    log_event("member.removed", status="ok", update_id=press.update_id,
              source=press.source, user_id=actor, duration_ms=ms_since(start),
              outcome="removed", deleted_entries=delete_entries)
    return target_id


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


REFUND_WORD = "refund"
REFUND_PREFIX = "rf:"

REFUND_USAGE = 'Say how much to refund — e.g. "refund 500".'
REFUND_BAD_AMOUNT = 'That doesn\'t look like an amount — try "refund 500".'
REFUND_NO_CANDIDATES = "I can't find a live expense that could take a refund like that."
REFUND_GONE = "That expense is gone — nothing to refund."
REFUND_OVER_LIMIT = "That's more than's left to refund on that expense."


def _is_refund(text: str) -> bool:
    """True when `text` opens with the bare word `refund` — deliberately not a
    slash command (§18): "refund 500" needs a predicate in `app.py`'s `is_parse`
    exclusion list, the same shape `/undo`/`/remove` already carve out of the
    LLM parse path and the daily cap, but with no leading `/` to match on."""
    words = text.split()
    return bool(words) and words[0].lower() == REFUND_WORD


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


def handle_confirm(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Confirm the pending transaction the Confirm tap carries, then acknowledge (§4).

    The tap carries the confirm card's message id; `confirm_pending` scopes the
    write to this user (§1) so one user's Confirm can't latch onto another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already stored) — either way we answer the callback query so Telegram
    clears the spinner. Does not commit — the caller owns the transaction.
    Returns the new `txn_id`, or `None` when there was nothing to confirm.

    On a real confirm the card is edited into a settled receipt with no keyboard
    (§4, §5): the only lasting evidence a transaction was saved is otherwise a
    toast that fades, and dropping the Confirm/Cancel buttons is also what stops a
    later stale Cancel from removing the receipt. A redelivered tap (`row is None`)
    leaves the already-settled card untouched.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    row = confirm_pending(conn, user_id, press.message_id,
                          source=press.source, update_id=press.update_id)
    if row is not None:
        edit_message_text(press.chat_id, press.message_id, settled_card(row))
    answer_callback_query(
        press.callback_query_id, "Saved ✅" if row else "Already saved"
    )
    if row is None:
        log_event("transaction.confirmed", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    log_event("transaction.confirmed", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start),
              txn_id=row["txn_id"], amount=row["amount"])
    return row["txn_id"]


def handle_cancel(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Discard the pending transaction the Cancel tap carries, then acknowledge (§5).

    The tap carries the confirm card's message id; `cancel_pending` scopes the
    delete to this user (§1) so one user's Cancel can't discard another's
    identically-numbered card. It returns `None` on a redelivered tap (the row
    is already gone) — either way we answer the callback query so Telegram
    clears the spinner. Nothing is written to the ledger. Does not commit — the
    caller owns the transaction. Returns the discarded `pending_id`, or `None`
    when there was nothing to cancel.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    pending_id = cancel_pending(conn, user_id, press.message_id)
    # Delete the card *only when this tap actually cancelled a pending row*. A
    # Cancel on an already-confirmed card finds no pending row (Confirm cleared it
    # and settled the card into a receipt), so deleting it would strip the receipt
    # from the chat while the transaction stays in the ledger — the transcript and
    # the ledger would then disagree. The toast still reports what happened, and
    # the user's own message stays — only the bot's live card goes. `delete_message`
    # returns False for a card older than the Bot API's 48-hour window; that just
    # leaves it in place, better than 500ing the tap into a redelivery loop.
    if pending_id is not None:
        delete_message(press.chat_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Discarded ❌" if pending_id else "Already gone"
    )
    log_event("pending.cancelled", status="ok" if pending_id else "noop",
              update_id=press.update_id, source=press.source, user_id=user_id,
              duration_ms=ms_since(start))
    return pending_id


def handle_category(conn: psycopg.Connection, press: ButtonPress) -> Transaction | None:
    """Apply a `cat:<name>` tap to the pending row, then re-render the card (§5).

    Category is the most-often-wrong field, so correcting it is one tap: the tap
    carries the card's message id and the chosen category, `set_pending_category`
    re-writes the pending row (scoped to this user, §1), and we edit the same card
    in place to reflect it — now a full confirm card, so a card that started as
    the null-category picker gains its Confirm/Cancel buttons.

    An unknown category (a forged callback the keyboard never emits) is ignored
    rather than 500ing — a raise here would make Telegram redeliver the bad tap
    forever. A stale card whose pending row is gone answers with a note and edits
    nothing. Does not commit — the caller owns the transaction. Returns the
    updated `Transaction`, or `None` when there was nothing to update.
    """
    start = time.perf_counter()
    category = press.data.removeprefix(CATEGORY_PREFIX)
    if category not in ALL_CATEGORIES:
        answer_callback_query(press.callback_query_id, "Unknown category")
        # A forged tap the keyboard never emits; user left unresolved on purpose,
        # so no `user_id` on this line.
        log_event("pending.recategorised", status="noop", update_id=press.update_id,
                  source=press.source, duration_ms=ms_since(start))
        return None
    user_id = get_or_create_user(conn, press.from_id)
    txn = set_pending_category(conn, user_id, press.message_id, category)
    if txn is None:
        answer_callback_query(press.callback_query_id, "That card's gone")
        log_event("pending.recategorised", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    text, keyboard = confirm_card(txn, list_accounts(conn, user_id))
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Category: {category}")
    log_event("pending.recategorised", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return txn


def handle_account_choice(conn: psycopg.Connection, press: ButtonPress) -> Transaction | None:
    """Apply an `acct:<name>` tap to the pending row, then re-render the card (§18, §5).

    The account picker's counterpart to `handle_category`: the tap carries the
    card's message id and the chosen account name, `set_pending_account`
    re-writes the pending row (scoped to this user, §1), and the same card is
    edited in place to reflect it. Unlike a category (a module-level closed
    set), the valid accounts are per household, so this handler resolves the
    user *first* and checks the name against that household's own
    `list_accounts` — a forged or stale account name is ignored rather than
    500ing, the same non-retry contract `handle_category` uses for a bad
    `cat:` tap. A stale card whose pending row is gone answers with a note and
    edits nothing. Does not commit — the caller owns the transaction. Returns
    the updated `Transaction`, or `None` when there was nothing to update.
    """
    start = time.perf_counter()
    account = press.data.removeprefix(ACCOUNT_PREFIX)
    user_id = get_or_create_user(conn, press.from_id)
    accounts = list_accounts(conn, user_id)
    if account not in accounts:
        answer_callback_query(press.callback_query_id, "Unknown account")
        log_event("pending.reaccounted", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    txn = set_pending_account(conn, user_id, press.message_id, account)
    if txn is None:
        answer_callback_query(press.callback_query_id, "That card's gone")
        log_event("pending.reaccounted", status="noop", update_id=press.update_id,
                  source=press.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    text, keyboard = confirm_card(txn, accounts)
    edit_message_text(press.chat_id, press.message_id, text, reply_markup=keyboard)
    answer_callback_query(press.callback_query_id, f"Account: {account}")
    log_event("pending.reaccounted", status="ok", update_id=press.update_id,
              source=press.source, user_id=user_id, duration_ms=ms_since(start))
    return txn


def handle_change_amount_request(conn: psycopg.Connection, press: ButtonPress) -> int | None:
    """Start "Change amount" on a recurring-rule card: ask for the new figure (§18).

    Marks the pending row `awaiting_amount` (`db.request_amount_change`) so the
    *next* text message this user sends is read as a replacement amount, not a
    new transaction — `app.py`'s webhook checks `pending_awaiting_amount` before
    routing a `TextMessage` anywhere else, which is what makes that distinction
    exist at all. The card itself is left untouched: Confirm and Skip still work
    while a reply is pending, and Skip is the escape hatch if the user changes
    their mind — it deletes the pending row outright, which clears the awaiting
    flag along with it. A stale tap (the card is already gone) is answered and
    ignored rather than 500ing, the same non-retry contract every other button
    handler here uses. Does not commit — the caller owns the transaction.
    Returns the marked `pending_id`, or `None` when there was nothing to mark.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, press.from_id)
    pending_id = request_amount_change(conn, user_id, press.message_id)
    answer_callback_query(
        press.callback_query_id, "Send the new amount" if pending_id else "That card's gone"
    )
    if pending_id is not None:
        send_message(press.chat_id, CHANGE_AMOUNT_PROMPT)
    log_event("pending.amount_change_requested", status="ok" if pending_id else "noop",
              update_id=press.update_id, source=press.source, user_id=user_id,
              duration_ms=ms_since(start))
    return pending_id


def handle_amount_reply(
    conn: psycopg.Connection, msg: TextMessage, telegram_message_id: int
) -> Transaction | None:
    """Apply a typed replacement amount, then re-render the card in place (§18).

    The other half of "Change amount": `app.py` resolves `telegram_message_id`
    via `db.pending_awaiting_amount` *before* dispatch, which is what routes
    this message here instead of `handle_text`'s ordinary parse — no LLM call,
    no pending row of its own. An unparseable reply (`money.parse_amount`
    raising) leaves the row still `awaiting_amount` so the user can just try
    again; Skip on the card remains the way out. On success the card is
    re-rendered with the same Skip label and "Change amount" button it had
    before — `set_pending_amount` only changes `amount`, so the category,
    account and note the model or an earlier tap already settled survive
    unchanged. Does not commit — the caller owns the transaction. Returns the
    updated `Transaction`, or `None` when the reply couldn't be read as an
    amount or the card is already gone.
    """
    start = time.perf_counter()
    user_id = get_or_create_user(conn, msg.from_id)
    try:
        amount = parse_amount(msg.text)
    except (TypeError, ValueError):
        send_message(msg.chat_id, CHANGE_AMOUNT_RETRY_PROMPT)
        log_event("pending.amount_changed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    txn = set_pending_amount(conn, user_id, telegram_message_id, amount)
    if txn is None:
        send_message(msg.chat_id, "That card's gone.")
        log_event("pending.amount_changed", status="noop", update_id=msg.update_id,
                  source=msg.source, user_id=user_id, duration_ms=ms_since(start))
        return None
    accounts = list_accounts(conn, user_id)
    text, keyboard = confirm_card(
        txn, accounts, cancel_label=SKIP_LABEL, change_amount_button=True
    )
    edit_message_text(msg.chat_id, telegram_message_id, text, reply_markup=keyboard)
    log_event("pending.amount_changed", status="ok", update_id=msg.update_id,
              source=msg.source, user_id=user_id, duration_ms=ms_since(start))
    return txn
