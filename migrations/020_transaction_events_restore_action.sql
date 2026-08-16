-- Undo a delete, rather than confirm it (DECISIONS §6, task 1771).
--
-- §6: soft delete "makes `/undo` itself reversible" — until now nothing
-- actually reversed it. The dashboard's delete toast offers an Undo that sets
-- `deleted_at` back to NULL; that restore is itself a money mutation (§17) and
-- needs its own audit value, or it would be indistinguishable from a `confirm`
-- of a brand-new row in the audit trail. Same two-liner as 013/014/018.
ALTER TABLE transaction_events DROP CONSTRAINT transaction_events_action_check;
ALTER TABLE transaction_events
    ADD CONSTRAINT transaction_events_action_check
    CHECK (action IN ('confirm', 'undo', 'delete', 'recategorise', 'edit', 'refund', 'adjustment', 'restore'));
