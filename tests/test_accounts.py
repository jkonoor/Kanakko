"""Derived account balances (DECISIONS §18).

`account_balances` recomputes `opening_balance + inflows − outflows` from the
ledger — never a stored total (§18) — and applies the credit sign convention: a
`credit` account reads as what is *owed*, not what it holds. This exercises the
whole formula against the applied schema: opening balances, income/expense in an
account, transfers between accounts, a soft-deleted row that must not count (§6),
and the credit negation. The `conn` fixture (a throwaway Postgres cluster) lives
in tests/conftest.py; each test ends with `conn.rollback()`.
"""

from decimal import Decimal

from conftest import household_of

from kanakko.categories import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from kanakko.db import account_balances, create_household_of_one, set_account_opening_balance
from kanakko.migrate import migrate
from kanakko.money import parse_amount


def _account(cur, hh, uid, *, kind, name, opening="0", is_default=False):
    # opening_balance is signed and may be negative (a credit account's debt) or
    # zero, both of which parse_amount refuses — so it takes a plain Decimal.
    cur.execute(
        "INSERT INTO accounts (household_id, owner, kind, name, opening_balance, is_default)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING account_id",
        (hh, uid, kind, name, Decimal(opening), is_default),
    )
    return cur.fetchone()[0]


def test_balances_derive_from_the_ledger_with_the_credit_sign_convention(conn):
    """`opening + inflows − outflows` per account, credit read as what is owed (§18).

    Seeds a household with three accounts and a mix of every money movement, then
    asserts each derived balance. The credit account is the sign-convention check:
    it starts owing ₹5,000 (a negative asset opening), a ₹2,000 swipe deepens the
    debt and a ₹3,000 bill payment (transfer into it) lightens it, and it must read
    back as **+4,000 owed**, not −4,000. Drop the credit negation in
    `account_balances` and that assertion reddens. A soft-deleted expense on Bank
    proves the read goes through `active_transactions` (§6): counted, Bank would be
    ₹9,999 short.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7201) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)

        bank = _account(cur, hh, uid, kind="spending", name="Bank",
                        opening="10000", is_default=True)
        cash = _account(cur, hh, uid, kind="spending", name="Cash")
        card = _account(cur, hh, uid, kind="credit", name="Card", opening="-5000")

        def txn(amount, type_, account=None, *, category=None, frm=None, to=None,
                deleted=False):
            cur.execute(
                "INSERT INTO transactions"
                " (user_id, household_id, amount, type, category, occurred_on,"
                "  account_id, from_account_id, to_account_id, deleted_at)"
                " VALUES (%s, %s, %s, %s, %s, '2026-08-05', %s, %s, %s,"
                "         %s)",
                (uid, hh, parse_amount(amount), type_, category, account, frm, to,
                 "2026-08-05" if deleted else None),
            )

        txn("2000", "expense", bank, category=EXPENSE_CATEGORIES[0])
        txn("500", "income", bank, category=INCOME_CATEGORIES[0])
        txn("2000", "expense", card, category=EXPENSE_CATEGORIES[0])   # swipe
        txn("3000", "transfer", frm=bank, to=card)                     # pay the bill
        txn("1000", "transfer", frm=bank, to=cash)                     # ATM
        txn("9999", "expense", bank, category=EXPENSE_CATEGORIES[0], deleted=True)

    balances = {a["name"]: a for a in account_balances(conn, uid)}

    # Bank: 10000 + 500 − 2000 − 3000 (→card) − 1000 (→cash); the deleted 9999 is
    # absent because the read goes through active_transactions (§6).
    assert balances["Bank"]["balance"] == Decimal("4500.00")
    # Cash: 0 + 1000 transferred in.
    assert balances["Cash"]["balance"] == Decimal("1000.00")
    # Card asset is −5000 − 2000 + 3000 = −4000; it reads as what is *owed*: +4000.
    assert balances["Card"]["balance"] == Decimal("4000.00")

    # Amounts are Decimal, never float (§9); default account first.
    assert all(isinstance(a["balance"], Decimal) for a in balances.values())
    assert account_balances(conn, uid)[0]["name"] == "Bank"


def test_balances_are_household_scoped(conn):
    """One household's accounts never see another's rows (§16)."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7202) RETURNING user_id")
        (mine,) = cur.fetchone()
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7203) RETURNING user_id")
        (theirs,) = cur.fetchone()
        hh_mine, hh_theirs = household_of(conn, mine), household_of(conn, theirs)
        my_acct = _account(cur, hh_mine, mine, kind="spending", name="Mine",
                           opening="100", is_default=True)
        _account(cur, hh_theirs, theirs, kind="spending", name="Theirs",
                 opening="999", is_default=True)
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, occurred_on, account_id)"
            " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05', %s)",
            (mine, hh_mine, parse_amount("40"), EXPENSE_CATEGORIES[0], my_acct),
        )

    mine_names = {a["name"] for a in account_balances(conn, mine)}
    assert mine_names == {"Mine"}
    assert account_balances(conn, mine)[0]["balance"] == Decimal("60.00")


def test_household_creation_mints_the_two_structural_accounts(conn):
    """`create_household_of_one` leaves a working bot with no onboarding answer (§18).

    A `spending` default ('Bank') — the pool a transaction naming no account lands
    in — and the structural `external` counterparty, both minted the moment the
    household exists, before anyone has typed `/account`. Dropping the
    `create_default_accounts` call from `create_household_of_one` would leave the
    first confirm with no default to stamp, which is exactly what this guards.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7301) RETURNING user_id")
        (uid,) = cur.fetchone()
    create_household_of_one(conn, uid)

    accounts = {a["kind"]: a for a in account_balances(conn, uid)}
    assert accounts.keys() == {"spending", "external"}
    assert accounts["spending"]["name"] == "Bank"
    assert accounts["spending"]["is_default"] is True
    assert accounts["spending"]["balance"] == Decimal("0.00")
    assert accounts["external"]["is_default"] is False
    conn.rollback()


def test_set_account_opening_balance_negates_credit_and_updates_on_a_repeat(conn):
    """§18: "same column, different question" — credit stores what is *owed* as a
    negative asset, `locked` stores what is already parked as a positive one. A
    second call (correcting a typo) updates the one account rather than minting a
    second `Card`, which the account_id staying the same across both calls proves.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7302) RETURNING user_id")
        (uid,) = cur.fetchone()
    create_household_of_one(conn, uid)

    card = set_account_opening_balance(conn, uid, "credit", parse_amount("5000"))
    savings = set_account_opening_balance(conn, uid, "locked", parse_amount("20000"))

    balances = {a["account_id"]: a for a in account_balances(conn, uid)}
    # Credit reads back as what is owed (positive) — stored internally as the
    # negative asset the sign-convention CASE in account_balances negates again.
    assert balances[card["account_id"]]["balance"] == Decimal("5000.00")
    assert balances[savings["account_id"]]["balance"] == Decimal("20000.00")

    corrected = set_account_opening_balance(conn, uid, "credit", parse_amount("4500"))
    assert corrected["account_id"] == card["account_id"]  # updated, not duplicated
    balances = {a["account_id"]: a for a in account_balances(conn, uid)}
    assert balances[card["account_id"]]["balance"] == Decimal("4500.00")
    assert len([a for a in balances.values() if a["kind"] == "credit"]) == 1
    conn.rollback()
