"""Users and update metering (§1, §14, §16).

The internal `users.user_id` every table keys on, plus the `processed_updates`
bookkeeping behind Telegram idempotency (§14) and the §16 daily cost cap.
"""

from datetime import date

import psycopg


def get_or_create_user(
    conn: psycopg.Connection, telegram_user_id: int, display_name: str | None = None
) -> int:
    """Resolve `telegram_user_id` to its internal `users.user_id`, creating it once.

    Every table keys on the internal `user_id` (§1), so the message handler turns
    the Telegram id it sees into that id here. `ON CONFLICT DO NOTHING` makes a
    second message from the same user a no-op insert rather than a unique
    violation; the `UNION ALL … LIMIT 1` then returns the existing row on that
    path. `display_name` is Telegram's `from.first_name` when the caller has it
    (§16, migration 022) — refreshed on every update so a rename shows up, and a
    no-op write when unchanged. Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH ins AS ("
            "  INSERT INTO users (telegram_user_id) VALUES (%s)"
            "  ON CONFLICT (telegram_user_id) DO NOTHING RETURNING user_id"
            ")"
            " SELECT user_id FROM ins"
            " UNION ALL"
            " SELECT user_id FROM users WHERE telegram_user_id = %s"
            " LIMIT 1",
            (telegram_user_id, telegram_user_id),
        )
        (user_id,) = cur.fetchone()
        # Kept fresh rather than written once: people rename themselves in
        # Telegram, and a roster showing a name they abandoned is worse than one
        # showing none. `IS DISTINCT FROM` makes the unchanged case — every
        # message after the first — touch no row at all. Optional because most
        # call sites resolve a user from an id they already have and have no name
        # to offer; the webhook passes it once per update at the §16 gate.
        if display_name is not None:
            cur.execute(
                "UPDATE users SET display_name = %s WHERE user_id = %s"
                " AND display_name IS DISTINCT FROM %s",
                (display_name, user_id, display_name),
            )
    return user_id


def find_user(conn: psycopg.Connection, telegram_user_id: int) -> int | None:
    """This Telegram id's internal `user_id`, or `None` — a pure lookup that
    creates nothing, unlike `get_or_create_user`.

    The Mini App routes resolve their caller through here (§16): a valid
    `initData` proves *which* Telegram user is asking, never that they are
    permitted, and `get_or_create_user` on that path would mint a `users` row for
    anyone who taps the menu button — a row that then satisfies the bot's own
    gate and belongs to no household.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM users WHERE telegram_user_id = %s"
            "   AND deleted_at IS NULL",
            (telegram_user_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def user_exists(conn: psycopg.Connection, telegram_user_id: int) -> bool:
    """True if a `users` row already exists for this Telegram id.

    The §16 authorization gate uses it to refuse an unrecognised user *before* any
    LLM call, without minting the very row §16 says must not be stored for a
    refused update. One definition, not two: it is `find_user` read as a boolean.
    """
    return find_user(conn, telegram_user_id) is not None


def claim_update(
    conn: psycopg.Connection, update_id: int, user_id: int | None
) -> bool:
    """Record `update_id` as processed by `user_id`; True the first time, False on a repeat (§14).

    Telegram redelivers any update it did not answer 2xx for, so the webhook calls
    this before running a handler and skips the handler when it returns False —
    the one place that makes every handler idempotent against redelivery. It
    matters most for `/undo`, which soft-deletes "the newest live row" with no
    per-message anchor (Confirm/Cancel key on the card's message id): a redelivery
    would otherwise soft-delete a *second* real transaction and drop it from every
    total. The INSERT runs in the caller's transaction, so the claim commits with
    the handler's writes and rolls back with them — a handler that 500s is
    retried. Does not commit — the caller owns the transaction.

    `user_id` stamps the row so `count_updates_on_day` can meter the §16 daily cost
    cap off this same table — but only for the metered (text-parse) path; the caller
    passes `None` for the free Confirm/Cancel/category/undo claims so their rows stay
    unmetered and the cap counts exactly the messages that cost an LLM call.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_updates (update_id, user_id) VALUES (%s, %s)"
            " ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
            (update_id, user_id),
        )
        return cur.fetchone() is not None


def count_updates_on_day(
    conn: psycopg.Connection, user_id: int, ist_day: date
) -> int:
    """How many updates this user has had handled on `ist_day` (§16 cost cap).

    The metering read behind the per-user daily message cap: each text-parse
    message is one LLM call (§2), so an unbounded user is an unbounded bill. Counts
    only the `processed_updates` rows stamped with this `user_id` — `claim_update`
    stamps it on the parse path alone, leaving the free Confirm/Cancel/category/undo
    claims NULL, so the `WHERE user_id = %s` counts exactly the LLM calls, not the
    free taps. Bucketed on `processed_at`
    **`AT TIME ZONE 'Asia/Kolkata'`** so the day rolls over at IST midnight, not
    UTC (§10) — a message at 23:50 IST counts against that IST day, not the next
    one it falls into in UTC. `ist_day` is the current IST date the caller
    computes, keeping the one clock read in the webhook. Reads the base table, not
    `active_transactions` — this counts updates, not ledger rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM processed_updates"
            " WHERE user_id = %s"
            " AND (processed_at AT TIME ZONE 'Asia/Kolkata')::date = %s",
            (user_id, ist_day),
        )
        (n,) = cur.fetchone()
    return n


def all_users(conn: psycopg.Connection) -> list[tuple[int, int]]:
    """Every user as `(user_id, telegram_user_id)` — the scheduled jobs' fan-out.

    The jobs read a user's ledger by internal `user_id` (§1) but send to the
    Telegram id; a private chat's `chat_id` is that Telegram id. Not a ledger
    read, so it goes straight to `users`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, telegram_user_id FROM users"
            " WHERE deleted_at IS NULL ORDER BY user_id"
        )
        return cur.fetchall()


def delete_account(conn: psycopg.Connection, user_id: int) -> str:
    """Erase this user's data and sever their identity (§16, migration 021).

    The way out of the *product*, distinct from `remove_member`'s way out of a
    *household*. Returns `"ok"`, or `"owner_must_transfer"` when they own a
    household someone else is still in — the same rule §16 already applies to
    leaving, for the same reason: a household always has an owner, and erasing
    one out from under its members is not the leaver's call to make. A **solo**
    owner is not blocked, and that is the point: they are the case that had no
    exit at all.

    What goes: every transaction they entered and its audit rows, their pending
    cards, reminder log, recurring rules, and their membership. If that empties
    the household, the household goes too, with its accounts and its invites.

    What stays: the `users` row, scrubbed. Eleven tables reference it and
    `invites.created_by` is NOT NULL on rows belonging to *other* people's
    history, so a cascade would delete evidence that is not this user's to take.
    `telegram_user_id` becomes `-user_id` — negative, so it can never match a
    real Telegram id again; unique, because `user_id` is; and it frees the real
    id so a later invite starts this person genuinely fresh.

    `processed_updates.user_id` is nulled rather than deleted: those rows are
    §14's redelivery guard, and dropping them would let Telegram replay an
    update this user's deletion just erased.

    Irreversible on purpose — no soft delete, no undo. Does not commit; the
    caller owns the transaction, so a failure part-way leaves the account whole.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hm.household_id, h.owner,"
            "  (SELECT count(*) FROM household_members WHERE household_id = hm.household_id)"
            " FROM household_members hm"
            " JOIN households h USING (household_id)"
            " WHERE hm.user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        household_id, owner, members = row if row else (None, None, 0)
        if owner == user_id and members > 1:
            return "owner_must_transfer"

        cur.execute(
            "DELETE FROM transaction_events WHERE txn_id IN"
            " (SELECT txn_id FROM transactions WHERE user_id = %s)",
            (user_id,),
        )
        for table in ("transactions", "pending_transactions", "reminder_log"):
            cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM recurring_rules WHERE created_by = %s", (user_id,))
        cur.execute(
            "UPDATE processed_updates SET user_id = NULL WHERE user_id = %s", (user_id,)
        )
        cur.execute("DELETE FROM household_members WHERE user_id = %s", (user_id,))

        if household_id is not None:
            cur.execute(
                "SELECT count(*) FROM household_members WHERE household_id = %s",
                (household_id,),
            )
            (remaining,) = cur.fetchone()
            if remaining == 0:
                # Nobody left to own it. Its rows go in FK order: invites and
                # recurring rules point at the household, accounts are pointed
                # at by transactions (already gone with their owner).
                for table in ("invites", "recurring_rules", "accounts"):
                    cur.execute(
                        f"DELETE FROM {table} WHERE household_id = %s", (household_id,)
                    )
                cur.execute(
                    "DELETE FROM households WHERE household_id = %s", (household_id,)
                )
            else:
                # A member leaving a household that lives on: anything of theirs
                # the household still needs is re-owned by whoever owns it now,
                # or the FK would point at a scrubbed row.
                cur.execute(
                    "UPDATE accounts SET owner = %s WHERE household_id = %s AND owner = %s",
                    (owner, household_id, user_id),
                )

        cur.execute(
            "UPDATE users SET telegram_user_id = -user_id, deleted_at = now()"
            " WHERE user_id = %s",
            (user_id,),
        )
    return "ok"
