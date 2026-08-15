-- Reconciliation adjustments (DECISIONS §18).
--
-- §18: "Weekly, per account... A different figure writes an adjustment row
-- against the external account... a visible ledger row, never a silent
-- correction." An adjustment is an ordinary `transfer` between the account and
-- `external` (011 already has the columns and the CHECK) — no schema change
-- needed there. The audit trail (§17) does need a value for it: writing it as
-- `action = 'confirm'` would make it indistinguishable from a user-typed
-- transfer in the audit query, and "visible... never silent" is the whole
-- point of this task. Same two-liner as 013/014.
ALTER TABLE transaction_events DROP CONSTRAINT transaction_events_action_check;
ALTER TABLE transaction_events
    ADD CONSTRAINT transaction_events_action_check
    CHECK (action IN ('confirm', 'undo', 'delete', 'recategorise', 'edit', 'refund', 'adjustment'));
