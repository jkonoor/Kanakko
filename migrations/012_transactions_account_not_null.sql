-- Enforce the account axis on every non-transfer transaction (DECISIONS §18).
--
-- Migration 010 added transactions.account_id nullable and backfilled it,
-- deferring enforcement until the write-wiring prerequisite existed. It now
-- does: every household-creation path (create_household_of_one, and the member
-- re-homing that calls it) mints a default `spending` account the moment the
-- household exists, and confirm_pending stamps every new row's account_id from
-- that default. With money always landing somewhere, a NULL account_id is a
-- bug, not an onboarding gap — mirroring 007→008's household_id enforcement.
--
-- A `transfer` (migration 011) is the one type this column cannot describe: it
-- names two accounts (from_account_id/to_account_id) and no single one, so a
-- blanket `SET NOT NULL` would force an arbitrary pick between them. This CHECK
-- exempts transfers instead of picking one — account_id stays NULL for a
-- transfer, and every other type must carry it.

ALTER TABLE transactions
    ADD CONSTRAINT transactions_account_id_required_check
    CHECK (account_id IS NOT NULL OR type = 'transfer');
