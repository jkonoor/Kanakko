"""Households, membership, and the §16 removal/transfer rules.

The tenancy axis §16 moves the ledger onto: a household owns the money, a member
enters it. Owner-only removal, self-removal, ownership transfer, and the roster
`/household` reads all live here — the authorization is in SQL and in
`_authorize_removal`, never at the call site.
"""

import psycopg


def create_household_of_one(conn: psycopg.Connection, user_id: int) -> int | None:
    """Home `user_id` in a new household of one, owned by themselves (§16).

    The onboarding counterpart to migration 006's backfill: a user minted after
    that migration (open-mode `/start`, or a consumed signup invite) has a `users`
    row but no household, and `transactions.household_id` is NOT NULL (migration
    008), so their first confirm would fail without this. Idempotent — a user who
    already belongs to a household is left where they are (the UNIQUE on
    `household_members.user_id` is the backstop), so a redelivered `/start` mints
    nothing new. Returns the new `household_id`, or `None` if already a member.
    Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO households (owner)"
            " SELECT %s WHERE NOT EXISTS ("
            "   SELECT 1 FROM household_members WHERE user_id = %s)"
            " RETURNING household_id",
            (user_id, user_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        (household_id,) = row
        cur.execute(
            "INSERT INTO household_members (household_id, user_id) VALUES (%s, %s)",
            (household_id, user_id),
        )
    return household_id


def household_roster(
    conn: psycopg.Connection, user_id: int
) -> list[tuple[int, bool, str | None]]:
    """The members of `user_id`'s household — `(member_user_id, is_owner, label)` (§16).

    Answers `/household`'s "who is in it, who owns it". Scoped on the household
    resolved from `user_id`'s `household_members` row, so it can never span
    households. Each member carries their household-invite `label` (`ravi`,
    `priya`) — the attribution §16 keeps so the operator can tell who is active;
    the owner joined by creating the household, not by an invite, so their label
    is NULL. The label join is scoped to *this* household (`i.household_id =
    m.household_id`): once a member can leave one household and join another
    (removal, §16) they carry a used household invite from each, and an unscoped
    join would stamp them with a stale label from the household they left. Owner
    first, then by join order. Empty when the user has no household
    (should not happen for an authorized user, but the caller handles it).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.user_id, hh.owner = m.user_id AS is_owner, i.label"
            " FROM household_members m"
            " JOIN households hh ON hh.household_id = m.household_id"
            " LEFT JOIN invites i"
            "   ON i.used_by = m.user_id AND i.kind = 'household'"
            "   AND i.household_id = m.household_id"
            " WHERE m.household_id ="
            "   (SELECT household_id FROM household_members WHERE user_id = %s)"
            " ORDER BY is_owner DESC, m.joined_at",
            (user_id,),
        )
        return [(uid, is_owner, label) for uid, is_owner, label in cur.fetchall()]


def _authorize_removal(
    conn: psycopg.Connection, actor_user_id: int, target_user_id: int
) -> tuple[str, int | None]:
    """Authorize a removal without acting — the one place the §16 rule lives, shared
    by `check_removal` and `remove_member`. Returns the verdict slug and, when it is
    `"ok"`, the household the removal is scoped to.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hh.household_id, hh.owner"
            " FROM household_members m"
            " JOIN households hh ON hh.household_id = m.household_id"
            " WHERE m.user_id = %s",
            (actor_user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "not_member", None
        household_id, owner = row
        cur.execute(
            "SELECT 1 FROM household_members"
            " WHERE household_id = %s AND user_id = %s",
            (household_id, target_user_id),
        )
        if cur.fetchone() is None:
            return "not_member", None
        if actor_user_id != owner and actor_user_id != target_user_id:
            return "not_owner", None
        if target_user_id == owner:
            return "owner_must_transfer", None
    return "ok", household_id


def check_removal(
    conn: psycopg.Connection, actor_user_id: int, target_user_id: int
) -> str:
    """Would removing `target` from `actor`'s household be permitted? (§16)

    The same authorization as `remove_member`, without acting, so `handle_remove`
    can present the retain/delete warning *only* when the removal will go through —
    and `remove_member` re-runs it when the button is tapped, since a callback is
    untrusted. Returns `"ok"`, `"not_owner"`, `"owner_must_transfer"`, or
    `"not_member"`.
    """
    return _authorize_removal(conn, actor_user_id, target_user_id)[0]


def remove_member(
    conn: psycopg.Connection,
    actor_user_id: int,
    target_user_id: int,
    *,
    delete_entries: bool = False,
) -> str:
    """Remove `target_user_id` from `actor_user_id`'s household (§16).

    One code path for both cases §16 permits — the owner removing any member, and
    a member removing themselves (the self case is `actor == target`). All
    authorization lives in `_authorize_removal`, not at the call site, so neither
    route nor a forged retain/delete button can bypass it:

    - a non-owner removing anyone but themselves is refused (`"not_owner"`);
    - removing the owner is refused (`"owner_must_transfer"`) — a household always
      has an owner (§16), so an owner must transfer ownership (a later task) before
      leaving. This also covers a solo user trying to leave their household of one.

    The removed member is re-homed into a fresh household of one, so their bot keeps
    working rather than 500ing on the next confirm (`transactions.household_id` is
    NOT NULL, migration 008).

    `delete_entries` decides the fate of the entries they logged into the household
    they leave — §16 asks retain-or-delete of them, with a warning, at the call
    site. Retain (the default) touches no `transactions` row: those rows carry that
    household's id and stay. Delete is real and irreversible — a hard `DELETE`,
    **not** §6's recoverable soft delete — and takes their audit rows with them (the
    `transaction_events` FK forbids orphaning them, and a deleted row has nothing
    left to audit). That is the "past reports stop reconciling" price §16 warns
    about, accepted knowingly. Does not commit — the caller owns the transaction.
    Returns `"removed"`, `"not_owner"`, `"owner_must_transfer"`, or `"not_member"`.
    """
    verdict, household_id = _authorize_removal(conn, actor_user_id, target_user_id)
    if verdict != "ok":
        return verdict
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM household_members WHERE household_id = %s AND user_id = %s",
            (household_id, target_user_id),
        )
        if delete_entries:
            cur.execute(
                "DELETE FROM transaction_events WHERE txn_id IN"
                " (SELECT txn_id FROM transactions"
                "  WHERE user_id = %s AND household_id = %s)",
                (target_user_id, household_id),
            )
            cur.execute(
                "DELETE FROM transactions WHERE user_id = %s AND household_id = %s",
                (target_user_id, household_id),
            )
    create_household_of_one(conn, target_user_id)
    return "removed"


def transfer_ownership(
    conn: psycopg.Connection, actor_user_id: int, target_user_id: int
) -> str:
    """Hand ownership of `actor`'s household to `target` (§16).

    A household always has an owner, so an owner cannot leave until they transfer
    ownership first — this is the operation that unblocks that `/remove`. Only the
    current owner may transfer, and only to another member of the same household.
    `target` carries an invite label, so `/transfer <label>` can never name the
    owner (they have none) — `already_owner` is the defensive verdict for a target
    that resolves back to the owner, which the label path cannot produce. Does not
    commit — the caller owns the transaction. Returns `"transferred"`,
    `"not_owner"`, `"not_member"`, or `"already_owner"`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hh.household_id, hh.owner"
            " FROM household_members m"
            " JOIN households hh ON hh.household_id = m.household_id"
            " WHERE m.user_id = %s",
            (actor_user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return "not_member"
        household_id, owner = row
        if actor_user_id != owner:
            return "not_owner"
        if target_user_id == owner:
            return "already_owner"
        cur.execute(
            "SELECT 1 FROM household_members"
            " WHERE household_id = %s AND user_id = %s",
            (household_id, target_user_id),
        )
        if cur.fetchone() is None:
            return "not_member"
        cur.execute(
            "UPDATE households SET owner = %s WHERE household_id = %s",
            (target_user_id, household_id),
        )
    return "transferred"
