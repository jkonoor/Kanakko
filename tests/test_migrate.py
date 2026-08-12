"""The runner against a real Postgres, which is the only thing that parses DDL.

`tests/test_migrations.py` reads the `.sql` files as text; this one executes
them, in a throwaway cluster booted in a tmp dir. It deliberately offers no
"point me at your own database" switch: the first assertion below only holds
against a virgin database, and `migrate()` commits, so a persistent DSN would
pass once and then fail on every later run.
"""

from datetime import date
from decimal import Decimal

import psycopg
import pytest
from conftest import household_of, join_household

from kanakko.categories import EXPENSE_CATEGORIES, INCOME_CATEGORIES
from kanakko.db import day_summary, month_summary
from kanakko.migrate import MIGRATIONS, migrate
from kanakko.money import parse_amount

# The `conn` fixture (a throwaway Postgres cluster) lives in tests/conftest.py.


def test_migrate_refuses_to_succeed_with_no_migrations(tmp_path, monkeypatch):
    """Finding nothing to apply is a packaging failure, not a clean run.

    `MIGRATIONS` resolves relative to the installed module, so a wheel built
    without `migrations/` makes the glob empty. Without this guard the migrator
    exits 0 against an empty schema and the deploy looks green. No server
    needed — the guard runs before the connection is touched, which is why this
    check can't be skipped on a machine with no Postgres.
    """
    monkeypatch.setattr("kanakko.migrate.MIGRATIONS", tmp_path)
    with pytest.raises(RuntimeError, match=str(tmp_path)):
        migrate(None)


def test_migrations_apply_and_are_recorded(conn):
    """The first run executes the DDL; the second must be a no-op.

    Re-running is the normal case — every container restart calls this — and a
    runner that forgets what it applied errors on the restart, not on the
    change that broke it.
    """
    names = sorted(sql.name for sql in MIGRATIONS.glob("*.sql"))
    assert migrate(conn) == names
    assert migrate(conn) == []

    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at, filename")
        assert [row[0] for row in cur.fetchall()] == names


def test_schema_stores_money_exactly(conn):
    """Round-trips through the applied schema, not through the .sql text.

    `NUMERIC(12, 2)` is only a promise until a server enforces it; a column
    that came back as float would round here and nowhere else.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (1) RETURNING user_id")
        (user_id,) = cur.fetchone()
        hid = household_of(conn, user_id)  # household_id is NOT NULL (migration 008)
        cur.execute(
            "INSERT INTO transactions (user_id, household_id, amount, type, occurred_on)"
            " VALUES (%s, %s, %s, 'expense', '2026-08-05') RETURNING txn_id",
            (user_id, hid, Decimal("1234.56")),
        )
        (txn_id,) = cur.fetchone()

        cur.execute("SELECT amount FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone() == (Decimal("1234.56"),)

        cur.execute("UPDATE transactions SET deleted_at = now() WHERE txn_id = %s", (txn_id,))
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (txn_id,))
        assert cur.fetchone() == (0,)
    conn.rollback()


def test_invite_kind_and_household_must_agree(conn):
    """The §16 invariant is structural, not a hope: a household invite carries a
    household, a signup invite carries none.

    Without the CHECK a mislabelled row grants the wrong thing at the gate — a
    signup code that quietly drops someone into a household, or a household code
    that joins nobody. The constraint only holds if the server enforces it, so
    this exercises the applied schema, both violating rows and both valid ones.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (7) RETURNING user_id")
        (uid,) = cur.fetchone()
        # A real household to reference — migration 006 added the FK on
        # invites.household_id, so a made-up id would now trip the FK, not the CHECK.
        cur.execute("INSERT INTO households (owner) VALUES (%s) RETURNING household_id", (uid,))
        (hh,) = cur.fetchone()

        # Valid: signup with no household, household with a household.
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " VALUES ('sig-1', 'signup', NULL, 'ravi', %s)",
            (uid,),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " VALUES ('hh-1', 'household', %s, 'priya', %s)",
            (hh, uid),
        )

        # Violations: signup with a (real) household, household with none — so the
        # CHECK is what fires, not the FK.
        for code, kind, hid in [("sig-2", "signup", hh), ("hh-2", "household", None)]:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO invites (code, kind, household_id, label, created_by)"
                    " VALUES (%s, %s, %s, 'x', %s)",
                    (code, kind, hid, uid),
                )
            conn.rollback()
    conn.rollback()


def test_household_backfill_migrates_seeded_users(conn):
    """Every pre-existing user ends up in exactly one household, owning it (§16).

    The backfill (migration 006) is exercised against a *seeded* multi-user
    database, not an empty one: three users are inserted first, then the backfill
    statement — the real SQL, read from the file, not a paraphrase — runs over
    them. A backfill that missed a user, double-homed one, or made someone else
    the owner would redden the exactly-one-self-owned assertion. Re-running it
    then proves idempotency: the WHERE NOT EXISTS guard plus the UNIQUE on
    household_members.user_id make the second pass a no-op, so a retried migration
    completes rather than duplicating households.
    """
    migrate(conn)
    text = (MIGRATIONS / "006_households.sql").read_text()
    backfill = "WITH new_households" + text.split("WITH new_households", 1)[1]
    with conn.cursor() as cur:
        seeded = []
        for tg in (9010, 9020, 9030):
            cur.execute("INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING user_id", (tg,))
            (uid,) = cur.fetchone()
            seeded.append(uid)

        cur.execute(backfill)

        for uid in seeded:
            cur.execute(
                "SELECT h.owner FROM households h"
                " JOIN household_members m USING (household_id)"
                " WHERE m.user_id = %s",
                (uid,),
            )
            assert cur.fetchall() == [(uid,)], f"user {uid} not in exactly one self-owned household"
        cur.execute("SELECT count(*) FROM households WHERE owner = ANY(%s)", (seeded,))
        assert cur.fetchone() == (len(seeded),)

        # Idempotent: a second backfill creates nothing for the already-homed users.
        cur.execute(backfill)
        cur.execute("SELECT count(*) FROM households WHERE owner = ANY(%s)", (seeded,))
        assert cur.fetchone() == (len(seeded),)
        cur.execute("SELECT count(*) FROM household_members WHERE user_id = ANY(%s)", (seeded,))
        assert cur.fetchone() == (len(seeded),)
    conn.rollback()


def test_transactions_household_backfill_preserves_totals(conn):
    """Moving the tenancy axis must not move a single rupee (§16).

    The dangerous migration: transactions gains household_id, backfilled from the
    household-of-one mapping. Exercised against a *seeded* multi-user ledger in
    the real deploy state — users and their transactions exist, migration 006's
    real backfill homes each user in a household of one, then 007's real backfill
    (read from the file, not paraphrased) stamps every transaction with its
    entering user's household. The proof the task demands: every pre-existing
    transaction lands in exactly one household's total, and each household's
    active total equals what its sole member spent before the column existed. A
    backfill that orphaned a row (NULL household), cross-homed one, or dropped or
    duplicated an amount reddens an assertion below — verified by breaking the
    UPDATE, not by reading it. A soft-deleted row is included: it must still gain
    a household (the column is on every row) yet stay out of the active total.
    """
    migrate(conn)
    hh006 = (MIGRATIONS / "006_households.sql").read_text()
    home_users = "WITH new_households" + hh006.split("WITH new_households", 1)[1]
    txn007 = (MIGRATIONS / "007_transactions_household.sql").read_text()
    home_txns = "UPDATE transactions t" + txn007.split("UPDATE transactions t", 1)[1].split(";", 1)[0]

    with conn.cursor() as cur:
        # Simulate the pre-007 world: transactions that predate the household axis
        # carry no household_id. Migration 008 (applied by migrate() above) forbids
        # that, so drop the NOT NULL for the length of this transaction — 007's
        # backfill (home_txns below) fills it, and the rollback at the end restores
        # the committed schema, keeping the module-scoped connection clean.
        cur.execute("ALTER TABLE transactions ALTER COLUMN household_id DROP NOT NULL")
        # Amounts chosen so a float sum would drift (10.10+20.20, 0.05+1.00).
        seeded = {}  # user_id -> Decimal active total
        for tg, amounts in [(9210, ["10.10", "20.20"]), (9220, ["0.05", "1.00"]), (9230, ["999.99"])]:
            cur.execute("INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING user_id", (tg,))
            (uid,) = cur.fetchone()
            for a in amounts:
                cur.execute(
                    "INSERT INTO transactions (user_id, amount, type, occurred_on)"
                    " VALUES (%s, %s, 'expense', '2026-08-05')",
                    (uid, parse_amount(a)),
                )
            seeded[uid] = sum((parse_amount(a) for a in amounts), Decimal("0"))

        # A soft-deleted row on the first user: must be homed, yet stay out of totals.
        first = next(iter(seeded))
        cur.execute(
            "INSERT INTO transactions (user_id, amount, type, occurred_on, deleted_at)"
            " VALUES (%s, %s, 'expense', '2026-08-05', now()) RETURNING txn_id",
            (first, parse_amount("500.00")),
        )
        (deleted_txn,) = cur.fetchone()

        cur.execute(home_users)  # migration 006: home the seeded users
        cur.execute(home_txns)   # migration 007: home their transactions
        seeded_ids = list(seeded)

        # No orphans: every transaction, deleted or not, now carries a household.
        cur.execute(
            "SELECT count(*) FROM transactions WHERE user_id = ANY(%s) AND household_id IS NULL",
            (seeded_ids,),
        )
        assert cur.fetchone() == (0,)

        # Each transaction is homed to its entering user's one household, never another's.
        cur.execute(
            "SELECT count(*) FROM transactions t JOIN household_members m ON m.user_id = t.user_id"
            " WHERE t.user_id = ANY(%s) AND t.household_id <> m.household_id",
            (seeded_ids,),
        )
        assert cur.fetchone() == (0,), "a transaction was homed to the wrong household"

        # The deleted row is homed but absent from the active view.
        cur.execute("SELECT household_id FROM transactions WHERE txn_id = %s", (deleted_txn,))
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT count(*) FROM active_transactions WHERE txn_id = %s", (deleted_txn,))
        assert cur.fetchone() == (0,)

        # Every household's active total equals its sole member's pre-migration spend,
        # summed through active_transactions — which must now expose household_id.
        cur.execute(
            "SELECT m.user_id, SUM(a.amount) FROM active_transactions a"
            " JOIN household_members m ON m.household_id = a.household_id"
            " WHERE m.user_id = ANY(%s) GROUP BY m.user_id",
            (seeded_ids,),
        )
        assert dict(cur.fetchall()) == seeded
    conn.rollback()


def test_transactions_household_id_is_not_null(conn):
    """Money must belong to a household — a NULL household_id insert is refused (§16).

    007 added the column nullable so the backfill could run; 008 makes it NOT NULL
    once the write path stamps every new row (confirm_pending). Without 008 an
    insert omitting the household silently orphans money from every household total
    rather than erroring — the exact silent-money bug §16 forbids. This exercises
    the applied schema: dropping `SET NOT NULL` from migration 008 reddens it.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (123) RETURNING user_id")
        (uid,) = cur.fetchone()
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, occurred_on)"
                " VALUES (%s, %s, 'expense', '2026-08-05')",
                (uid, Decimal("1.00")),
            )
    conn.rollback()


def test_accounts_external_backfill_homes_every_household(conn):
    """Every household gets exactly one `external` account, owned by its owner (§18).

    §18 makes the `external` account the counterparty for opening balances and
    adjustments, so it is structural. The backfill (migration 009) is exercised
    against a *seeded* multi-household database, not the virgin one migrate() ran
    on: three households are created first, then 009's real INSERT (read from the
    file, not paraphrased) runs over them. A backfill that missed a household, made
    two, or set the wrong owner reddens an assertion. Re-running proves idempotency:
    the NOT EXISTS guard makes the second pass a no-op.
    """
    migrate(conn)
    text = (MIGRATIONS / "009_accounts.sql").read_text()
    backfill = "INSERT INTO accounts" + text.split("INSERT INTO accounts", 1)[1]
    with conn.cursor() as cur:
        owners = []
        for tg in (9310, 9320, 9330):
            cur.execute("INSERT INTO users (telegram_user_id) VALUES (%s) RETURNING user_id", (tg,))
            (uid,) = cur.fetchone()
            cur.execute("INSERT INTO households (owner) VALUES (%s) RETURNING household_id", (uid,))
            (hh,) = cur.fetchone()
            owners.append((uid, hh))

        cur.execute(backfill)

        for uid, hh in owners:
            cur.execute(
                "SELECT owner, kind, name FROM accounts WHERE household_id = %s AND kind = 'external'",
                (hh,),
            )
            assert cur.fetchall() == [(uid, "external", "External")], f"household {hh}"

        # Idempotent: a second pass mints no duplicate.
        cur.execute(backfill)
        hhs = [hh for _, hh in owners]
        cur.execute(
            "SELECT count(*) FROM accounts WHERE household_id = ANY(%s) AND kind = 'external'",
            (hhs,),
        )
        assert cur.fetchone() == (len(hhs),)
    conn.rollback()


def test_accounts_one_live_default_per_household(conn):
    """The partial UNIQUE index lets a household have at most one live default (§18).

    §18 pins "one default per household" to a structural constraint, not app code.
    The index is partial on `is_default AND deleted_at IS NULL`, so: a second live
    default in the same household is refused, two households may each have one, and
    a soft-deleted former default must not block naming a new one. Dropping the
    index from migration 009 reddens the first assertion.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9410) RETURNING user_id")
        (uid,) = cur.fetchone()
        cur.execute("INSERT INTO households (owner) VALUES (%s) RETURNING household_id", (uid,))
        (hh_a,) = cur.fetchone()
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9420) RETURNING user_id")
        (uid_b,) = cur.fetchone()
        cur.execute("INSERT INTO households (owner) VALUES (%s) RETURNING household_id", (uid_b,))
        (hh_b,) = cur.fetchone()

        def add(hh, owner, is_default):
            cur.execute(
                "INSERT INTO accounts (household_id, owner, kind, name, is_default)"
                " VALUES (%s, %s, 'spending', 'Bank', %s) RETURNING account_id",
                (hh, owner, is_default),
            )
            return cur.fetchone()[0]

        first_default = add(hh_a, uid, True)
        add(hh_a, uid, False)  # a non-default is fine alongside it
        add(hh_b, uid_b, True)  # another household's default is fine

        # A second live default in household A is refused. A savepoint, not a full
        # rollback, so the seeded households above survive the expected failure.
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            add(hh_a, uid, True)

        # A soft-deleted former default does not block a new one.
        cur.execute("UPDATE accounts SET deleted_at = now() WHERE account_id = %s", (first_default,))
        add(hh_a, uid, True)
    conn.rollback()


def test_account_kind_check_and_signed_opening_balance(conn):
    """kind is a closed set, and a credit account's opening balance can be negative (§18).

    §18: a `credit` account's opening balance is what is *owed*, so the column is
    signed — a `CHECK (amount > 0)` here (copied from `transactions`) would be
    wrong. It round-trips through the applied NUMERIC(12,2), which would round a
    float. And a kind outside the four is a hard error, not a silent free-text
    pool the reports would ignore.
    """
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9510) RETURNING user_id")
        (uid,) = cur.fetchone()
        cur.execute("INSERT INTO households (owner) VALUES (%s) RETURNING household_id", (uid,))
        (hh,) = cur.fetchone()

        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name, opening_balance)"
            " VALUES (%s, %s, 'credit', 'HDFC card', %s) RETURNING account_id",
            (hh, uid, Decimal("-12345.67")),
        )
        (acc,) = cur.fetchone()
        cur.execute("SELECT opening_balance FROM accounts WHERE account_id = %s", (acc,))
        assert cur.fetchone() == (Decimal("-12345.67"),)

        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            cur.execute(
                "INSERT INTO accounts (household_id, owner, kind, name)"
                " VALUES (%s, %s, 'savings', 'nope')",
                (hh, uid),
            )
    conn.rollback()


def test_account_backfill_leaves_every_report_number_unchanged(conn):
    """Adding account_id and backfilling moves no report number (§18).

    §18 relocates every pre-existing row into its household's default account — a
    pure placement, not a re-scoping. So `month_summary` must read identically
    before and after 010's account-creation + backfill run. The check is a
    before/after comparison across the migration: seed a two-member household with
    income, expenses across categories, and a soft-deleted row, record the summary,
    run 010's real statements (read from the file, not paraphrased), record it
    again, and assert equality. A backfill that dropped a row, double-counted one,
    or a view left frozen at 007 (so account_id never surfaces) would still pass
    this — the summary reads `active_transactions` — which is the point: the
    migration must be invisible to the totals. The row-level half asserts the
    placement actually happened: every seeded row now points at the one live
    default, none left NULL.
    """
    migrate(conn)
    text = (MIGRATIONS / "010_transactions_account.sql").read_text()
    make_default = "INSERT INTO accounts" + text.split("INSERT INTO accounts", 1)[1].split(";", 1)[0]
    backfill = "UPDATE transactions t" + text.split("UPDATE transactions t", 1)[1].split(";", 1)[0]

    food, groceries = EXPENSE_CATEGORIES[0], EXPENSE_CATEGORIES[1]
    salary = INCOME_CATEGORIES[0]
    first, last = date(2026, 8, 1), date(2026, 9, 1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9610) RETURNING user_id")
        (owner,) = cur.fetchone()
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9620) RETURNING user_id")
        (member,) = cur.fetchone()
        hh = household_of(conn, owner)
        join_household(conn, hh, member)

        # Amounts chosen so a float sum would drift. A soft-deleted row is seeded so
        # the "unchanged" claim covers the view's filter, not just the sum.
        rows = [
            (owner, "expense", food, "10.10"),
            (owner, "expense", groceries, "20.20"),
            (member, "expense", food, "0.05"),
            (member, "income", salary, "999.99"),
        ]
        for uid, typ, cat, raw in rows:
            cur.execute(
                "INSERT INTO transactions (user_id, household_id, amount, type, category, occurred_on)"
                " VALUES (%s, %s, %s, %s, %s, '2026-08-05')",
                (uid, hh, parse_amount(raw), typ, cat),
            )
        cur.execute(
            "INSERT INTO transactions (user_id, household_id, amount, type, category, occurred_on, deleted_at)"
            " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05', now())",
            (owner, hh, parse_amount("500.00"), food),
        )

        before = month_summary(conn, owner, first, last)

        cur.execute(make_default)  # 010: mint the household's default account
        cur.execute(backfill)      # 010: home every row into it

        after = month_summary(conn, owner, first, last)
        assert after == before, "the account backfill changed a report number"

        # Every seeded row is homed to the one live default; none left NULL.
        cur.execute(
            "SELECT count(*) FROM transactions WHERE household_id = %s AND account_id IS NULL", (hh,)
        )
        assert cur.fetchone() == (0,), "a transaction was left without an account"
        cur.execute(
            "SELECT DISTINCT account_id FROM transactions WHERE household_id = %s", (hh,)
        )
        placed = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT account_id FROM accounts"
            " WHERE household_id = %s AND is_default AND deleted_at IS NULL",
            (hh,),
        )
        assert placed == [cur.fetchone()[0]], "rows not homed to the household's default account"
    conn.rollback()


def test_transfer_is_excluded_from_spending_and_income_totals(conn):
    """A transfer between two of a household's accounts moves neither total (§18).

    §18: a transfer is "excluded from every spending and income total". The
    exclusion is structural — `day_summary` and `month_summary` sum with positive
    `type = 'expense'` / `type = 'income'` filters (db/reports.py:37,66,78), so a
    `transfer` row is invisible to both. This proves it against the applied schema:
    seed an expense, an income, and a ₹5,000 transfer between the household's two
    accounts, then assert the summaries read back exactly the expense and the
    income — the transfer counted for nothing. Broadening any one of those three
    filters to catch a transfer (dropping a FILTER, or `type <> 'income'`) reddens
    an assertion here. The 011 CHECK invariant is exercised too: a transfer must
    name both ends, and no other type may name either.
    """
    migrate(conn)
    first, last = date(2026, 8, 1), date(2026, 9, 1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (9710) RETURNING user_id")
        (uid,) = cur.fetchone()
        hh = household_of(conn, uid)

        def account(name):
            cur.execute(
                "INSERT INTO accounts (household_id, owner, kind, name)"
                " VALUES (%s, %s, 'spending', %s) RETURNING account_id",
                (hh, uid, name),
            )
            return cur.fetchone()[0]

        bank, cash = account("Bank"), account("Cash")

        # An expense and an income the totals must count.
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, occurred_on, account_id)"
            " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05', %s)",
            (uid, hh, parse_amount("300.00"), EXPENSE_CATEGORIES[0], bank),
        )
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, category, occurred_on, account_id)"
            " VALUES (%s, %s, %s, 'income', %s, '2026-08-05', %s)",
            (uid, hh, parse_amount("1000.00"), INCOME_CATEGORIES[0], bank),
        )
        # A ₹5,000 transfer Bank → Cash — neither spending nor income (§18).
        cur.execute(
            "INSERT INTO transactions"
            " (user_id, household_id, amount, type, occurred_on, from_account_id, to_account_id)"
            " VALUES (%s, %s, %s, 'transfer', '2026-08-05', %s, %s)",
            (uid, hh, parse_amount("5000.00"), bank, cash),
        )

    # The transfer counted for nothing in either total.
    _, spent, received = day_summary(conn, uid, date(2026, 8, 5))
    assert spent == Decimal("300.00"), "transfer leaked into the spend total"
    assert received == Decimal("1000.00"), "transfer leaked into the income total"
    income, expenses, _ = month_summary(conn, uid, first, last)
    assert expenses == Decimal("300.00"), "transfer leaked into the month's expenses"
    assert income == Decimal("1000.00"), "transfer leaked into the month's income"

    with conn.cursor() as cur:
        # The invariant: a transfer must name both ends...
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            cur.execute(
                "INSERT INTO transactions"
                " (user_id, household_id, amount, type, occurred_on, from_account_id)"
                " VALUES (%s, %s, %s, 'transfer', '2026-08-05', %s)",
                (uid, hh, parse_amount("1.00"), bank),
            )
        # ...and no other type may name either end.
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            cur.execute(
                "INSERT INTO transactions"
                " (user_id, household_id, amount, type, category, occurred_on, account_id, to_account_id)"
                " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05', %s, %s)",
                (uid, hh, parse_amount("1.00"), EXPENSE_CATEGORIES[0], bank, cash),
            )
    conn.rollback()


def test_parse_store_sum_by_category_stays_exact(conn):
    """The whole money path — parse → store → SUM(amount) GROUP BY category.

    The amounts are chosen so a `float` pipeline would drift: 0.10 + 0.20 + 0.30
    is 0.6000000000000001 in binary floating point, and 10.10 + 20.20 + 0.05 is
    30.349999999999998. Through `parse_amount` (Decimal) into `NUMERIC(12, 2)`
    and back through Postgres SUM, both come back exact. A column that had become
    float, or a Python-side float sum, reddens the equality — and summing over
    `active_transactions` (never `transactions`, DECISIONS §6) means a
    soft-deleted row must not land in a category total.
    """
    migrate(conn)
    food, groceries = EXPENSE_CATEGORIES[0], EXPENSE_CATEGORIES[1]
    entries = [
        (food, "₹0.10"),
        (food, "0.20"),
        (food, "0.30"),
        (groceries, "10.10"),
        (groceries, "20.20"),
        (groceries, "0.05"),
    ]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (2) RETURNING user_id")
        (user_id,) = cur.fetchone()
        hid = household_of(conn, user_id)  # household_id is NOT NULL (migration 008)
        for category, raw in entries:
            cur.execute(
                "INSERT INTO transactions (user_id, household_id, amount, type, category, occurred_on)"
                " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
                (user_id, hid, parse_amount(raw), category),
            )
        # A soft-deleted Food row must not reach the Food total.
        cur.execute(
            "INSERT INTO transactions (user_id, household_id, amount, type, category, occurred_on)"
            " VALUES (%s, %s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
            (user_id, hid, parse_amount("999.99"), food),
        )
        (deleted_txn,) = cur.fetchone()
        cur.execute("UPDATE transactions SET deleted_at = now() WHERE txn_id = %s", (deleted_txn,))

        cur.execute(
            "SELECT category, SUM(amount) FROM active_transactions"
            " WHERE user_id = %s GROUP BY category",
            (user_id,),
        )
        totals = dict(cur.fetchall())

    assert totals == {food: Decimal("0.60"), groceries: Decimal("30.35")}
    assert all(isinstance(total, Decimal) for total in totals.values())
    conn.rollback()
