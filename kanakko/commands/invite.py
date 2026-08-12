"""`/invite` and `/invite_signup` — mint single-use links onto a household or the
bot itself (§16).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 4/6). Both
commands share this module because they share a shape: a labelled, single-use
`invites` row and a `https://t.me/<bot>?start=<code>` deep link, differing only
in who may issue one and what the code admits into. `_command_arg`/`TextMessage`
stay in `handlers.py` (the shared spine every command module needs); importing
them here is cheaper than a second copy.
"""

import secrets
import time

import psycopg

from kanakko.auth import is_admin
from kanakko.db import create_household_invite, create_signup_invite, get_or_create_user
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import TextMessage, _command_arg
from kanakko.tg import get_bot_username, send_message

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
