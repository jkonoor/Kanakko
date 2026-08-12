"""`/remove` — remove a household member, retaining or deleting their entries (§16).

Split out of `kanakko/handlers.py` (task: `handlers.py` split, slice 3/6).
`_command_arg`/`TextMessage`/`ButtonPress` stay in `handlers.py` (the shared
spine every command module needs); importing them here is cheaper than a
second copy.
"""

import time

import psycopg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko.db import check_removal, get_or_create_user, household_roster, remove_member
from kanakko.eventlog import log_event, ms_since
from kanakko.handlers import ButtonPress, TextMessage, _command_arg
from kanakko.tg import answer_callback_query, edit_message_text, send_message

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
