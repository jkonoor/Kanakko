"""The confirm flow: pending rows, confirm, cancel, category correction, undo.

A parsed transaction lives as a `pending_transactions` row from the moment its
confirm card is sent until the user taps Confirm or Cancel: `save_pending` writes
that row, `confirm_pending` turns it into a real `transactions` row and clears
the pending one, `cancel_pending` discards it, `undo_last` walks back the newest
confirmed entry.

Amounts are serialized as strings and read back through the `Transaction` model,
whose validator routes them through `money.parse_amount` (§9) — a JSON number (a
float) would be refused there, so a float can never reach the ledger.
"""

from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from kanakko.db.audit import _record_event
from kanakko.parse import Transaction


def save_pending(
    conn: psycopg.Connection,
    user_id: int,
    telegram_message_id: int,
    txn: Transaction,
    recurring_rule_id: int | None = None,
) -> int:
    """Store `txn` as a pending row keyed by the confirm card's message id.

    `model_dump(mode="json")` serializes `amount` as a string and `date` as ISO,
    so nothing float-shaped is persisted; `confirm_pending` reverses it through
    the same model. `recurring_rule_id` names the rule whose cron send produced
    this card (§18) — `None` for the ordinary parse path, which is every caller
    but `jobs.recurring`. Kept as its own column rather than a field on `txn`:
    it is provenance about *why* the card exists, not part of the parsed
    transaction. Returns the new `pending_id`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pending_transactions"
            " (user_id, telegram_message_id, parsed, recurring_rule_id)"
            " VALUES (%s, %s, %s, %s) RETURNING pending_id",
            (user_id, telegram_message_id, Jsonb(txn.model_dump(mode="json")), recurring_rule_id),
        )
        (pending_id,) = cur.fetchone()
    return pending_id


def confirm_pending(
    conn: psycopg.Connection,
    user_id: int,
    telegram_message_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Write user's pending row for `telegram_message_id` to `transactions`, clear it.

    Scoped by `user_id` because Telegram message ids repeat per chat, not
    globally (§1): keying on the message id alone would let one user's Confirm
    write another user's pending transaction. `save_pending` stores `user_id`;
    this reverses it with the same scoping.

    Carries the pending row's `recurring_rule_id` (§18) straight onto the stored
    row, unchanged — this function has no idea whether the card came from a
    typed message or `jobs.recurring`'s cron send, and does not need to: the
    `None` every ordinary confirm carries is just another value here.

    The row is homed in the entering user's household (§16): `household_id` is the
    `household_members` row for `user_id`, the tenancy axis §16 moves off the user.
    A user with no membership yields NULL and the NOT NULL constraint (migration
    008) refuses the insert rather than orphaning money from every household total.
    `account_id` resolves `txn.account` by name within the confirmer's household
    (§18); a null `txn.account` — the model couldn't tell, or nobody changed the
    card's default — falls back to the household's default account, `coalesce`d
    in the same query. A household with no default (a test fixture that bypassed
    onboarding) yields NULL, which `accounts.account_id` still permits until the
    NOT NULL write-wiring task lands.

    A `transfer` (§18) takes the other branch: `account_id`/`category` are NULL
    and `from_account_id`/`to_account_id` resolve `txn.from_account`/`to_account`
    by name instead — mirroring migration 011's CHECK, so a swipe (an ordinary
    expense on the `credit` account) and its bill payment (a transfer into it)
    never both count as spending. A transfer naming `txn.new_locked_account`
    instead of `to_account` (§18: a locked pool mentioned for the first time,
    e.g. "put 5000 in SIP") mints that `locked` account here — get-or-create, not
    a bare INSERT, so a stale card racing a housemate's identically-named pool
    reuses it rather than minting a duplicate.

    The read → insert → delete → audit run in one transaction so a crash can never
    store a transaction while leaving its pending row live (a later double
    confirm), nor clear the pending row with nothing stored, nor write the ledger
    row without its audit trail (§17). `source`/`update_id` are the acting update's
    correlation id, taken as arguments so the audit write cannot come apart from
    the money write (§17). Returns the stored row — `txn_id` plus its fields, so
    the caller can log the amount (§17) — or `None` when there is no pending row
    (Telegram redelivers taps it already got a 200 for, so confirming twice must
    not write the transaction twice). Does not commit — the caller owns the
    transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed, recurring_rule_id FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed, recurring_rule_id = row
        txn = Transaction.model_validate(parsed)
        cur.execute(
            "SELECT household_id FROM household_members WHERE user_id = %s", (user_id,)
        )
        home = cur.fetchone()
        household_id = home[0] if home is not None else None
        if txn.type == "transfer":
            # §18: a transfer names its two ends and no single account_id/category —
            # migration 011's CHECK enforces the same shape on the row.
            account_id = category = None
            cur.execute(
                "SELECT account_id FROM accounts"
                " WHERE household_id = %s AND name = %s AND deleted_at IS NULL",
                (household_id, txn.from_account),
            )
            from_row = cur.fetchone()
            from_account_id = from_row[0] if from_row else None
            if txn.new_locked_account is not None:
                # §18 (investment accounts): the pool named on the card doesn't
                # exist until this Confirm — get-or-create rather than a bare
                # INSERT, since a stale card (accounts fetched before a
                # housemate created the same-named pool) must not mint a
                # duplicate.
                cur.execute(
                    "SELECT account_id FROM accounts"
                    " WHERE household_id = %s AND name = %s AND deleted_at IS NULL",
                    (household_id, txn.new_locked_account),
                )
                new_row = cur.fetchone()
                if new_row is not None:
                    to_account_id = new_row[0]
                else:
                    cur.execute(
                        "INSERT INTO accounts (household_id, owner, kind, name)"
                        " VALUES (%s, %s, 'locked', %s) RETURNING account_id",
                        (household_id, user_id, txn.new_locked_account),
                    )
                    (to_account_id,) = cur.fetchone()
            else:
                cur.execute(
                    "SELECT account_id FROM accounts"
                    " WHERE household_id = %s AND name = %s AND deleted_at IS NULL",
                    (household_id, txn.to_account),
                )
                to_row = cur.fetchone()
                to_account_id = to_row[0] if to_row else None
        else:
            cur.execute(
                "SELECT coalesce("
                "  (SELECT account_id FROM accounts"
                "    WHERE household_id = %s AND name = %s AND deleted_at IS NULL),"
                "  (SELECT account_id FROM accounts a"
                "    WHERE a.household_id = %s AND a.is_default AND a.deleted_at IS NULL)"
                ")",
                (household_id, txn.account, household_id),
            )
            (account_id,) = cur.fetchone()
            from_account_id = to_account_id = None
            category = txn.category
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, account_id, amount, type, category, note, occurred_on,"
            "  from_account_id, to_account_id, recurring_rule_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING txn_id",
            (user_id, household_id, account_id, txn.amount, txn.type, category, txn.note, txn.date,
             from_account_id, to_account_id, recurring_rule_id),
        )
        (txn_id,) = cur.fetchone()
        cur.execute("DELETE FROM pending_transactions WHERE pending_id = %s", (pending_id,))
        stored = {
            "amount": txn.amount,
            "type": txn.type,
            "category": category,
            "note": txn.note,
            "occurred_on": txn.date,
            "from_account": txn.from_account,
            # A new pool's name lived in `new_locked_account`, not `to_account`,
            # until the INSERT above minted it — the settled receipt still names
            # it as the transfer's destination.
            "to_account": txn.to_account or txn.new_locked_account,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="confirm",
                      before=None, after=stored, source=source, update_id=update_id)
    return {"txn_id": txn_id, **stored}


def set_pending_category(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, category: str
) -> Transaction | None:
    """Set `category` on the user's pending row for `telegram_message_id` (§5).

    The correction path for the most-often-wrong field: a `cat:<name>` tap
    re-writes the pending row's category and returns the updated `Transaction`
    so the handler can re-render the card. Scoped by `user_id` like the confirm/
    cancel reads — message ids repeat per chat (§1). Round-trips through the
    `Transaction` model, so `category` is re-validated against the closed set
    (§11) and the amount stays a string on the way back to JSONB (§9). Returns
    `None` when there is no pending row (a stale card). Does not commit — the
    caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed = row
        txn = Transaction.model_validate({**parsed, "category": category})
        cur.execute(
            "UPDATE pending_transactions SET parsed = %s WHERE pending_id = %s",
            (Jsonb(txn.model_dump(mode="json")), pending_id),
        )
    return txn


def set_pending_account(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, account: str
) -> Transaction | None:
    """Set `account` on the user's pending row for `telegram_message_id` (§18, §5).

    `set_pending_category`'s counterpart for the account picker: an `acct:<name>`
    tap re-writes the pending row's account and returns the updated `Transaction`
    so the handler can re-render the card. Scoped by `user_id` for the same
    reason (§1). Trusts the caller has already checked `account` against the
    confirmer's own household accounts (`handle_account_choice` does, since the
    closed set is per household rather than a module-level constant like
    category's) — unlike `set_pending_category`, round-tripping through
    `Transaction` here re-validates nothing, because `_account_is_known` only
    fires when validation context carries the account list. Returns `None` when
    there is no pending row (a stale card). Does not commit — the caller owns the
    transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed = row
        txn = Transaction.model_validate({**parsed, "account": account})
        cur.execute(
            "UPDATE pending_transactions SET parsed = %s WHERE pending_id = %s",
            (Jsonb(txn.model_dump(mode="json")), pending_id),
        )
    return txn


def request_amount_change(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int
) -> int | None:
    """Mark the user's pending row `awaiting_amount` (§18).

    `handlers.handle_change_amount_request`'s write, fired by a "Change amount"
    tap on a recurring-rule card: the flag is what makes the *next* text message
    this user sends get read as a replacement amount rather than a new
    transaction — `app.py`'s webhook checks `pending_awaiting_amount` before
    routing a `TextMessage` anywhere else. Scoped by `user_id` like every other
    pending write here (§1). Returns the marked `pending_id`, or `None` when
    there is no pending row for this card (a stale or already-settled tap).
    Does not commit — the caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_transactions SET awaiting_amount = true"
            " WHERE user_id = %s AND telegram_message_id = %s RETURNING pending_id",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def pending_awaiting_amount(conn: psycopg.Connection, user_id: int) -> int | None:
    """The `telegram_message_id` of the user's card currently awaiting a
    replacement amount (§18), or `None` when nothing is waiting.

    `app.py`'s webhook calls this for every `TextMessage` before deciding where
    to route it — a hit means the message in hand is a reply to "Change
    amount", not a transaction to parse. Most-recent-first like the other
    pending lookups, in case more than one card is unusually awaiting an amount
    at once.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id FROM pending_transactions"
            " WHERE user_id = %s AND awaiting_amount"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def set_pending_amount(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int, amount: Decimal
) -> Transaction | None:
    """Apply a replacement amount to the user's awaiting pending row (§18).

    `handlers.handle_amount_reply`'s write: "Change amount" only ever asks for a
    new figure, never a full re-parse, so only `amount` changes — the rest of
    the parsed row (category, account, note, date) survives untouched, the same
    round-trip-through-`Transaction` shape `set_pending_category`/
    `set_pending_account` use, re-validating the amount through
    `money.parse_amount` (§9) on the way in. Scoped to a row that is still
    `awaiting_amount`, so a stray message arriving after the card was cancelled
    or confirmed elsewhere cannot silently rewrite a row that is no longer
    waiting for one. Clears the flag on success. Returns `None` when there is no
    such row. Does not commit — the caller owns the transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT pending_id, parsed FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s AND awaiting_amount"
            " ORDER BY created_at DESC, pending_id DESC LIMIT 1",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        pending_id, parsed = row
        txn = Transaction.model_validate({**parsed, "amount": str(amount)})
        cur.execute(
            "UPDATE pending_transactions SET parsed = %s, awaiting_amount = false"
            " WHERE pending_id = %s",
            (Jsonb(txn.model_dump(mode="json")), pending_id),
        )
    return txn


def undo_last(
    conn: psycopg.Connection,
    user_id: int,
    *,
    source: str,
    update_id: int | None,
) -> dict | None:
    """Soft-delete the user's most recent confirmed transaction, return its fields (§5, §6).

    `/undo` corrects the last entry: it sets `deleted_at` on the newest live row
    rather than hard-deleting it, so the ledger stays recoverable (§6). The row is
    chosen from `active_transactions` — the view that already hides soft-deleted
    rows — so a *second* `/undo` walks back to the previous entry instead of
    re-deleting the one just removed (reading `transactions` directly would keep
    latching onto the already-deleted newest row). Scoped by `user_id` — undo
    removes *your own* last entry, never a housemate's (§16) — and additionally by
    the household the user belongs to, so no read of the view ever spans households
    (§1, §16).
    The soft-delete and its audit row (§17) share one `conn.transaction()`, so a
    crash between them is impossible; `source`/`update_id` are the correlation id
    of the update doing the undo. Returns the removed row's fields — including its
    `txn_id` (§17) — for the confirmation reply and the event log, or `None` when
    there is no live transaction to undo. Does not commit — the caller owns the
    transaction.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE transactions SET deleted_at = now()"
            " WHERE txn_id = ("
            "   SELECT txn_id FROM active_transactions"
            "   WHERE user_id = %s"
            "   AND household_id = (SELECT household_id FROM household_members WHERE user_id = %s)"
            "   ORDER BY created_at DESC, txn_id DESC LIMIT 1"
            " )"
            " RETURNING txn_id, amount, type, category, note, occurred_on",
            (user_id, user_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        txn_id, amount, type_, category, note, occurred_on = row
        removed = {
            "amount": amount,
            "type": type_,
            "category": category,
            "note": note,
            "occurred_on": occurred_on,
        }
        _record_event(cur, txn_id=txn_id, user_id=user_id, action="undo",
                      before=removed, after=None, source=source, update_id=update_id)
    return {"txn_id": txn_id, **removed}


def cancel_pending(
    conn: psycopg.Connection, user_id: int, telegram_message_id: int
) -> int | None:
    """Discard the user's pending row for `telegram_message_id`, storing nothing (§5).

    Scoped by `user_id` for the same reason as `confirm_pending` — message ids
    repeat per chat, so a Cancel keyed on the id alone could delete another
    user's pending card. Returns the deleted `pending_id`, or `None` when there
    is nothing to cancel (a redelivered tap Telegram already got a 200 for), so
    the handler can tell a fresh cancel from a repeat. Does not commit — the
    caller owns the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pending_transactions"
            " WHERE user_id = %s AND telegram_message_id = %s RETURNING pending_id",
            (user_id, telegram_message_id),
        )
        row = cur.fetchone()
    return row[0] if row else None
