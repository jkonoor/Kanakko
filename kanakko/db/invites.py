"""Invite issue and consumption — the deep-link half of onboarding (§16).

Two kinds kept distinct on purpose: a signup invite joins nobody and opens a
household of one, a household invite implies signup and adds the user to the
inviting owner's household. Every code is single-use.
"""

import psycopg

from kanakko.db.households import create_household_of_one
from kanakko.db.users import get_or_create_user


def consume_invite(
    conn: psycopg.Connection, code: str, telegram_user_id: int
) -> str:
    """Consume an invite `code` for a Telegram user, admitting them (§16).

    The deep-link half of onboarding: a signup invite opens a household of one, a
    household invite adds the user to the invite's household — either way the code
    is single-use and stamped spent. Returns one of `"ok"`, `"spent"`, `"expired"`,
    `"unknown"` so `/start` can refuse each distinctly.

    Nothing is stored on a refused code (§16 — not even a user row): validity is
    checked *before* `get_or_create_user`, so a spent, expired, or unknown code
    mints no user, household, or membership. A redelivery whose code this same
    user already consumed returns `"ok"` idempotently, not a spurious `"spent"`.
    The claim (`UPDATE … WHERE used_by IS NULL`) settles two simultaneous
    consumers — the loser gets `"spent"`. Does not commit — the caller owns the
    transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT invite_id, kind, household_id, used_by,"
            " (expires_at IS NOT NULL AND expires_at < now()) AS expired"
            " FROM invites WHERE code = %s",
            (code,),
        )
        row = cur.fetchone()
        if row is None:
            return "unknown"
        invite_id, kind, household_id, used_by, expired = row
        if used_by is not None:
            cur.execute(
                "SELECT user_id FROM users WHERE telegram_user_id = %s",
                (telegram_user_id,),
            )
            existing = cur.fetchone()
            if existing is not None and existing[0] == used_by:
                return "ok"  # this user already consumed it — a redelivery
            return "spent"
        if expired:
            return "expired"
        user_id = get_or_create_user(conn, telegram_user_id)
        cur.execute(
            "UPDATE invites SET used_by = %s, used_at = now()"
            " WHERE invite_id = %s AND used_by IS NULL",
            (user_id, invite_id),
        )
        if cur.rowcount == 0:
            return "spent"  # a concurrent consumer won the race
        if kind == "household":
            cur.execute(
                "INSERT INTO household_members (household_id, user_id)"
                " VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                (household_id, user_id),
            )
        else:
            create_household_of_one(conn, user_id)
    return "ok"


def create_household_invite(
    conn: psycopg.Connection, owner_user_id: int, code: str, label: str
) -> bool:
    """Issue a single-use household invite for the household `owner_user_id` owns (§16).

    Owner-only, enforced in SQL: the `SELECT … FROM households WHERE owner = %s`
    yields the owner's own household or nothing, so a member who isn't the owner
    inserts no row and gets `False` — the guard the check proves. The code is
    `household` kind (implies signup, §16), labelled so the operator can tell who
    is active. Returns `True` if issued, `False` if the user owns no household.
    Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " SELECT %s, 'household', household_id, %s, %s"
            " FROM households WHERE owner = %s",
            (code, label, owner_user_id, owner_user_id),
        )
        return cur.rowcount == 1


def create_signup_invite(
    conn: psycopg.Connection, created_by_user_id: int, code: str, label: str
) -> None:
    """Issue a single-use signup invite — admits a new user with their own household (§16).

    The other half of §16's two grants: `create_household_invite` adds someone to an
    existing household, this one admits a stranger to the bot and `consume_invite`
    gives them a household of one. `household_id` is NULL, which the
    `invites_household_matches_kind` CHECK requires of the signup kind.

    No permission check here, unlike its household sibling: ownership is a fact this
    table can answer in SQL, but "is an operator" is not — it lives in the
    environment (`auth.is_admin`), so the handler is the only place that can gate it.
    Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " VALUES (%s, 'signup', NULL, %s, %s)",
            (code, label, created_by_user_id),
        )
