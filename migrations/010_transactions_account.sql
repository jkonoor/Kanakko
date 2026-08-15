-- Give every transaction a place (DECISIONS §18).
--
-- §18: money has a location. Migration 009 added the accounts table and each
-- household's structural `external` account; this puts an `account_id` on every
-- transaction. A pre-existing row names no account, so each household first gets
-- a default `spending` account and every one of its rows is backfilled to it —
-- "a transaction that names no account lands [in the default]" (§18). This is a
-- pure relocation into an account that sums identically: no report number moves,
-- which is the property the before/after `month_summary` check asserts.
--
-- NOT NULL is deferred, exactly as 007 deferred it to 008. Enforcing account_id
-- NOT NULL requires every household-creation path (onboarding's
-- create_household_of_one, member re-homing) to mint a default account, and the
-- write path (confirm_pending) to stamp it — none of which exist yet (the
-- onboarding-accounts task lands the default-account creation). Enforcing it now
-- would break the first confirm of every household minted after this migration.
-- This is the nullable + backfill half; enforcement is the write-wiring half.

-- Each existing household gets a default `spending` account, owned by the
-- household owner — the pool a transaction naming no account lands in (§18).
-- Named `Bank`, not `Cash`: every pre-existing row lands here, and that history
-- is mostly UPI and card, which §18 says pull from the bank. Neither name is
-- true of all of it, but `Cash` would mislabel the majority on screen.
-- Idempotent: a re-run skips households that already have a live default (the
-- partial UNIQUE index from 009 would reject a duplicate anyway).
INSERT INTO accounts (household_id, owner, kind, name, is_default)
SELECT h.household_id, h.owner, 'spending', 'Bank', true
FROM households h
WHERE NOT EXISTS (
    SELECT 1 FROM accounts a
    WHERE a.household_id = h.household_id AND a.is_default AND a.deleted_at IS NULL
);

ALTER TABLE transactions
    ADD COLUMN account_id BIGINT REFERENCES accounts (account_id);

-- Backfill: every row lands in its household's default account. The join keys on
-- the household the row already carries (007/008), so it cannot move a row
-- between households. Idempotent by the account_id IS NULL guard: a re-run over
-- already-placed rows changes nothing.
UPDATE transactions t
SET account_id = a.account_id
FROM accounts a
WHERE a.household_id = t.household_id
  AND a.is_default
  AND a.deleted_at IS NULL
  AND t.account_id IS NULL;

-- Recreate the view: `SELECT *` froze the column list at 007, so account_id is
-- invisible to active_transactions until the view is rebuilt (DECISIONS §6). The
-- soft-delete filter is unchanged.
CREATE OR REPLACE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
