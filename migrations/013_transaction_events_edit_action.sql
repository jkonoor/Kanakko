-- Widen the audit action set: dashboard field edits (DECISIONS §17, task 974).
--
-- §5 rejected a field editor in the bot chat but named "the Mini App transaction
-- list" as the cheaper replacement for anything older than the last entry. The
-- dashboard already writes `recategorise` for a category change; amount, date,
-- note and account each need the same audit trail, or a money-changing edit has
-- no before/after (§17). One new action covers all four — the changed field name
-- lives in `before`/`after` (`{"amount": ...}`, `{"occurred_on": ...}`, etc.), so a
-- second action per field would only fragment the same audit story.
--
-- 003 created the check inline, so Postgres named it
-- `transaction_events_action_check` (the deterministic `{table}_{column}_check`
-- form 011 already relies on for `transactions_type_check`).
ALTER TABLE transaction_events DROP CONSTRAINT transaction_events_action_check;
ALTER TABLE transaction_events
    ADD CONSTRAINT transaction_events_action_check
    CHECK (action IN ('confirm', 'undo', 'delete', 'recategorise', 'edit'));
