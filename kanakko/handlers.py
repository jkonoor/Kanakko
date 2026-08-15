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
import string
import time
from dataclasses import dataclass

import httpx
import psycopg
from pydantic import ValidationError

from kanakko.auth import signup_mode
from kanakko.confirm import category_prompt, confirm_card
from kanakko.db import (
    consume_invite,
    create_household_of_one,
    get_or_create_user,
    household_roster,
    list_accounts,
    save_pending,
    undo_last,
    user_exists,
)
from kanakko.eventlog import log_event, ms_since
from kanakko.money import format_amount
from kanakko.parse import build_request, parse_message, resolve_model
from kanakko.tg import send_message
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

    `reply_to_message_id` is Telegram's own `reply_to_message.message_id` when
    the user tapped "Reply" on a specific message, `None` for a bare message.
    It's how a reconcile reply is tied to the nudge it answers (§18) rather than
    guessed at by recency.
    """

    chat_id: int
    message_id: int
    text: str
    from_id: int | None = None
    update_id: int | None = None
    source: str = "webhook"
    reply_to_message_id: int | None = None

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
            reply_to_message_id=(message.get("reply_to_message") or {}).get("message_id"),
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
