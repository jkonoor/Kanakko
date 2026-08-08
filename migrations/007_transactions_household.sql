-- Move the ledger's tenancy axis onto every transaction (DECISIONS §16).
--
-- §16: a household owns the money, a user owns their entries. `transactions`
-- gains household_id (whose money) while user_id stays as who entered it. Every
-- pre-existing row is backfilled from the household-of-one mapping migration 006
-- built, so no transaction changes which totals it lands in — it simply gains
-- the household column that already described it implicitly.
--
-- The column is nullable here: the write path (confirm) and reads are re-scoped
-- to the household in the following tasks, and until confirm supplies a
-- household_id a NOT NULL would break every insert. Enforcement lands with the
-- write re-scoping that populates it.

ALTER TABLE transactions
    ADD COLUMN household_id BIGINT REFERENCES households (household_id);

-- Backfill from the one membership each user has. Migration 006's UNIQUE on
-- household_members.user_id guarantees exactly one household per user, so this
-- cannot fan a transaction out into two households. Idempotent by the
-- household_id IS NULL guard: a re-run over already-homed rows changes nothing.
UPDATE transactions t
SET household_id = m.household_id
FROM household_members m
WHERE m.user_id = t.user_id
  AND t.household_id IS NULL;

-- The report index follows the tenancy axis: reads re-scope to the household, so
-- the live-rows lookup keys on (household_id, occurred_on), mirroring the
-- per-user index migration 001 built for the pre-household read path.
CREATE INDEX transactions_household_occurred_on_idx
    ON transactions (household_id, occurred_on)
    WHERE deleted_at IS NULL;

-- Recreate the view: `SELECT *` froze the column list at migration 001, so the
-- new column is invisible to active_transactions until the view is rebuilt — and
-- every read goes through this view (DECISIONS §6), so a household predicate
-- would have nothing to filter on otherwise. The soft-delete filter is unchanged.
CREATE OR REPLACE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
