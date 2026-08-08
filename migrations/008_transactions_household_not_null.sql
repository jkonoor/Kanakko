-- Enforce the tenancy axis on every transaction (DECISIONS §16).
--
-- Migration 007 added transactions.household_id nullable and backfilled the rows
-- that already existed; the write path (confirm_pending) now stamps every new row
-- with the entering user's household. With both old and new money homed, the
-- column becomes NOT NULL — a transaction with no household is money that belongs
-- to no one, which §16 forbids, and it would silently vanish from every
-- household's total rather than error. 007 deliberately left this off so the
-- backfill and the ~dozen inserts that omitted the column could be updated first;
-- this is the enforcement half of that split.

ALTER TABLE transactions
    ALTER COLUMN household_id SET NOT NULL;
