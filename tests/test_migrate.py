"""The runner against a real Postgres, which is the only thing that parses DDL.

`tests/test_migrations.py` reads the `.sql` files as text; this one executes
them, in a throwaway cluster booted in a tmp dir. It deliberately offers no
"point me at your own database" switch: the first assertion below only holds
against a virgin database, and `migrate()` commits, so a persistent DSN would
pass once and then fail on every later run.
"""

from decimal import Decimal

import psycopg
import pytest

from kanakko.categories import EXPENSE_CATEGORIES
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
        cur.execute(
            "INSERT INTO transactions (user_id, amount, type, occurred_on)"
            " VALUES (%s, %s, 'expense', '2026-08-05') RETURNING txn_id",
            (user_id, Decimal("1234.56")),
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
        for category, raw in entries:
            cur.execute(
                "INSERT INTO transactions (user_id, amount, type, category, occurred_on)"
                " VALUES (%s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
                (user_id, parse_amount(raw), category),
            )
        # A soft-deleted Food row must not reach the Food total.
        cur.execute(
            "INSERT INTO transactions (user_id, amount, type, category, occurred_on)"
            " VALUES (%s, %s, 'expense', %s, '2026-08-05') RETURNING txn_id",
            (user_id, parse_amount("999.99"), food),
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
