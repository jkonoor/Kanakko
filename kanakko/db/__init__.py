"""Database access for Kanakko (DECISIONS §1, §6, §7, §9, §16, §17).

Plain SQL via psycopg, no ORM (§7). Split by responsibility once §16's households,
memberships and invites pushed the single `db.py` past 600 lines (CLAUDE.md pins
the trigger to Phase 9): connection, users + metering, households, invites, the
confirm flow, household-scoped reads/reports, reminders, and the audit write. The
import surface is unchanged — `from kanakko.db import <name>` still resolves every
function, re-exported here.
"""

from kanakko.db.accounts import (
    account_balances,
    list_accounts,
    set_account_opening_balance,
)
from kanakko.db.connection import connect
from kanakko.db.households import (
    check_removal,
    create_household_of_one,
    household_roster,
    remove_member,
    transfer_ownership,
)
from kanakko.db.invites import (
    consume_invite,
    create_household_invite,
    create_signup_invite,
)
from kanakko.db.pending import (
    cancel_pending,
    confirm_pending,
    save_pending,
    set_pending_category,
    undo_last,
)
from kanakko.db.reminders import last_reminder_at, log_reminder, logged_since
from kanakko.db.reports import (
    day_summary,
    month_summary,
    recent_transactions,
    set_transaction_category,
    soft_delete_transaction,
)
from kanakko.db.users import (
    all_users,
    claim_update,
    count_updates_on_day,
    find_user,
    get_or_create_user,
    user_exists,
)

__all__ = [
    "connect",
    "account_balances",
    "list_accounts",
    "set_account_opening_balance",
    "get_or_create_user",
    "find_user",
    "user_exists",
    "claim_update",
    "count_updates_on_day",
    "all_users",
    "create_household_of_one",
    "household_roster",
    "check_removal",
    "remove_member",
    "transfer_ownership",
    "consume_invite",
    "create_household_invite",
    "create_signup_invite",
    "save_pending",
    "confirm_pending",
    "set_pending_category",
    "undo_last",
    "cancel_pending",
    "day_summary",
    "month_summary",
    "recent_transactions",
    "soft_delete_transaction",
    "set_transaction_category",
    "logged_since",
    "log_reminder",
    "last_reminder_at",
]
