-- Tie a transaction back to the recurring rule that spawned it (DECISIONS §18).
--
-- §18: "On the day, the cron sends the ordinary confirm card... reuses the
-- entire §4 confirm machinery." 015's comment anticipated this: "No transaction
-- row references a rule yet... a nullable recurring_rule_id column." The link
-- has to survive the gap between the cron send and whenever the user actually
-- taps a button — hours, maybe days — so both `pending_transactions` (the
-- card's lifetime) and `transactions` (the settled row, for the future account-
-- acting note in 05f28e5's review) get the column.
--
-- ON DELETE SET NULL, not a plain FK: `delete_recurring_rule` is a real DELETE
-- (015 — "a standing instruction, not a ledger row"), and once a rule has sent
-- even one confirmed transaction, a bare FK would turn every future delete into
-- an IntegrityError. The transaction itself is real money that happened; it
-- must survive the rule being removed.

ALTER TABLE pending_transactions
    ADD COLUMN recurring_rule_id BIGINT REFERENCES recurring_rules (rule_id) ON DELETE SET NULL;

ALTER TABLE transactions
    ADD COLUMN recurring_rule_id BIGINT REFERENCES recurring_rules (rule_id) ON DELETE SET NULL;

-- Recreate the view: `SELECT *` froze the column list at its last CREATE OR
-- REPLACE (010, 014), so recurring_rule_id is invisible to active_transactions
-- until the view is rebuilt (DECISIONS §6). The soft-delete filter is unchanged.
CREATE OR REPLACE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
