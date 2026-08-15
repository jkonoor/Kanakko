"""Reconciliation adjustments: a visible ledger row, never a silent correction (§18).

`create_adjustment` compares a reported balance against the account's derived
figure (`accounts.account_balances`'s own formula) and, on a mismatch, writes an
ordinary `transfer` against the household's `external` account plus its audit
row. The guard named by the task: the adjustment must show up in *both* the
ledger and `transaction_events` — a test that only re-reads the balance
afterwards would pass on a version that quietly UPDATEd `opening_balance`
instead of writing a visible row, which is exactly the failure mode §18 warns
against.
"""

from datetime import date
from decimal import Decimal

from conftest import household_of

from kanakko.db import (
    account_balances,
    accounts_for_reconcile,
    clear_reconcile_ask,
    create_adjustment,
    create_reconcile_ask,
    pending_awaiting_reconcile,
)
from kanakko.migrate import migrate

OCCURRED = date(2026, 8, 13)


def _account(cur, hh, uid, *, kind, name, opening="0", is_default=False):
    if is_default:
        cur.execute(
            "UPDATE accounts SET name = %s, opening_balance = %s"
            " WHERE household_id = %s AND is_default AND deleted_at IS NULL"
            " RETURNING account_id",
            (name, Decimal(opening), hh),
        )
        return cur.fetchone()[0]
    cur.execute(
        "INSERT INTO accounts (household_id, owner, kind, name, opening_balance, is_default)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING account_id",
        (hh, uid, kind, name, Decimal(opening), is_default),
    )
    return cur.fetchone()[0]


def _events(conn, txn_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, before, after, source, update_id"
            " FROM transaction_events WHERE txn_id = %s ORDER BY created_at, event_id",
            (txn_id,),
        )
        return cur.fetchall()


def _row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — fixed test-only table names
        return cur.fetchone()[0]


def test_a_matching_report_writes_nothing(conn):
    """No drift, no row — an adjustment for zero disagreement would say nothing."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8601) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="1000", is_default=True)

    before_txns = _row_count(conn, "transactions")
    before_events = _row_count(conn, "transaction_events")

    result = create_adjustment(
        conn, uid, bank, Decimal("1000.00"), OCCURRED, source="webhook", update_id=1
    )

    assert result is None
    assert _row_count(conn, "transactions") == before_txns
    assert _row_count(conn, "transaction_events") == before_events
    conn.rollback()


def test_a_different_figure_writes_a_visible_transfer_and_its_audit_row(conn):
    """The named guard: the adjustment lands in the ledger *and* the audit trail.

    Bank's derived balance is ₹1,000; the user reports ₹1,200 — money the ledger
    doesn't know about. Asserts the stored transfer (external → Bank, ₹200), its
    `action="adjustment"` audit row (distinct from a user-typed "confirm"), and
    that the derived balance now reads the reported figure.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8602) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="1000", is_default=True)

    result = create_adjustment(
        conn, uid, bank, Decimal("1200.00"), OCCURRED, source="webhook", update_id=42
    )

    assert result is not None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT type, amount, from_account_id, to_account_id, note"
            " FROM transactions WHERE txn_id = %s",
            (result["txn_id"],),
        )
        type_, amount, from_id, to_id, note = cur.fetchone()
    assert type_ == "transfer"
    assert amount == Decimal("200.00")
    assert from_id != bank and to_id == bank  # external → Bank: money appeared
    assert note == "Reconciliation adjustment"

    events = _events(conn, result["txn_id"])
    assert len(events) == 1
    action, before, after, source, update_id = events[0]
    assert action == "adjustment"
    assert before is None
    assert after is not None
    assert source == "webhook"
    assert update_id == 42

    balances = {a["account_id"]: a for a in account_balances(conn, uid)}
    assert balances[bank]["balance"] == Decimal("1200.00")
    conn.rollback()


def test_a_lower_report_transfers_out_to_external(conn):
    """The other direction: reported balance below the ledger's figure."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8603) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="1000", is_default=True)

    result = create_adjustment(
        conn, uid, bank, Decimal("800.00"), OCCURRED, source="webhook", update_id=1
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, from_account_id, to_account_id FROM transactions WHERE txn_id = %s",
            (result["txn_id"],),
        )
        amount, from_id, to_id = cur.fetchone()
    assert amount == Decimal("200.00")
    assert from_id == bank and to_id != bank  # Bank → external: money is missing

    balances = {a["account_id"]: a for a in account_balances(conn, uid)}
    assert balances[bank]["balance"] == Decimal("800.00")
    conn.rollback()


def test_credit_account_sign_convention(conn):
    """A card owing more than the ledger thought deepens the debt, not lightens it.

    Card presents as owing ₹500 (opening asset −500). The user reports owing
    ₹700 — drift is "owed more" — which must move the *same direction* an
    ordinary swipe already moves the account (a transfer out of the card, to
    external), so the adjustment composes with account_balances's existing
    negation instead of needing a special case there. Dropping the credit
    negation from create_adjustment's query would flip this transfer's
    direction and this assertion would catch it.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8604) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        card = _account(cur, hh, uid, kind="credit", name="Card", opening="-500")

    result = create_adjustment(
        conn, uid, card, Decimal("700.00"), OCCURRED, source="webhook", update_id=1
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount, from_account_id, to_account_id FROM transactions WHERE txn_id = %s",
            (result["txn_id"],),
        )
        amount, from_id, to_id = cur.fetchone()
    assert amount == Decimal("200.00")
    assert from_id == card and to_id != card  # deepens the debt, like a swipe would

    balances = {a["account_id"]: a for a in account_balances(conn, uid)}
    assert balances[card]["balance"] == Decimal("700.00")  # reads as what is owed
    conn.rollback()


def test_household_scoping_refuses_a_foreign_account(conn):
    """One household's account can never be reconciled by another's member (§16)."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8605) RETURNING user_id")
        (mine,) = cur.fetchone()
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8606) RETURNING user_id")
        (theirs,) = cur.fetchone()
        household_of(conn, mine)
        hh_theirs = household_of(conn, theirs)
        their_bank = _account(cur, hh_theirs, theirs, kind="spending", name="Theirs",
                              opening="1000", is_default=True)

    before = _row_count(conn, "transactions")
    result = create_adjustment(
        conn, mine, their_bank, Decimal("5000.00"), OCCURRED, source="webhook", update_id=1
    )

    assert result is None
    assert _row_count(conn, "transactions") == before
    conn.rollback()


def test_the_external_account_itself_cannot_be_reconciled(conn):
    """`external` is structural bookkeeping — never something a nudge asks about."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8607) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        cur.execute(
            "SELECT account_id FROM accounts WHERE household_id = %s AND kind = 'external'",
            (hh,),
        )
        (external,) = cur.fetchone()

    result = create_adjustment(
        conn, uid, external, Decimal("999.00"), OCCURRED, source="webhook", update_id=1
    )
    assert result is None
    conn.rollback()


def test_accounts_for_reconcile_lists_every_household_and_excludes_external(conn):
    """The weekly cron's read: cross-household, `external` never included (§18)."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8608) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        card = _account(cur, hh, uid, kind="credit", name="Card", opening="-500")

    accounts = {a["account_id"]: a for a in accounts_for_reconcile(conn)}
    with conn.cursor() as cur:
        cur.execute("SELECT account_id FROM accounts WHERE household_id = %s", (hh,))
        all_ids = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT account_id FROM accounts WHERE household_id = %s AND kind = 'external'", (hh,)
        )
        (external,) = cur.fetchone()

    assert external not in accounts  # structural, never nudged
    assert card in accounts
    assert accounts[card]["telegram_user_id"] == 8608
    assert accounts[card]["kind"] == "credit"
    assert all_ids - {external} <= accounts.keys()  # every reconcilable account is present
    conn.rollback()


def test_reconcile_ask_round_trip(conn):
    """`create_reconcile_ask` → `pending_awaiting_reconcile` → `clear_reconcile_ask` (§18).

    The read must find nothing before the ask is created, find it after, and find
    nothing again once it's cleared — the same lifecycle `pending_awaiting_amount`
    has for "Change amount".
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8609) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="1000", is_default=True)

    assert pending_awaiting_reconcile(conn, uid) is None

    pending_id = create_reconcile_ask(conn, uid, 9001, bank)
    assert pending_id is not None
    assert pending_awaiting_reconcile(conn, uid) == {
        "telegram_message_id": 9001, "account_id": bank,
    }

    assert clear_reconcile_ask(conn, uid, 9001) == pending_id
    assert pending_awaiting_reconcile(conn, uid) is None
    assert clear_reconcile_ask(conn, uid, 9001) is None  # already gone
    conn.rollback()


def test_pending_awaiting_reconcile_does_not_misroute_across_two_asks(conn):
    """`104d562` review finding 1: a household with two outstanding asks (Bank and
    Card) must never route a reply to "whichever nudge is newest". A reply that
    names its nudge (`reply_to_message_id`) resolves to that exact ask, even
    though it's the *older* of the two; a bare reply with no nudge named and
    more than one ask outstanding resolves to neither, rather than guessing.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (8610) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)
        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="1000", is_default=True)
        card = _account(cur, hh, uid, kind="credit", name="Card", opening="-500")

    create_reconcile_ask(conn, uid, 9101, bank)  # sent first
    create_reconcile_ask(conn, uid, 9102, card)  # sent second — the LIFO trap

    # Replying to the Bank nudge specifically must resolve to Bank, not the
    # newer Card ask a plain "most recent" pick would have returned.
    assert pending_awaiting_reconcile(conn, uid, reply_to_message_id=9101) == {
        "telegram_message_id": 9101, "account_id": bank,
    }
    assert pending_awaiting_reconcile(conn, uid, reply_to_message_id=9102) == {
        "telegram_message_id": 9102, "account_id": card,
    }
    # A reply aimed at neither nudge matches nothing.
    assert pending_awaiting_reconcile(conn, uid, reply_to_message_id=404) is None
    # No reply target and two outstanding asks: refuse to guess.
    assert pending_awaiting_reconcile(conn, uid) is None
    conn.rollback()
