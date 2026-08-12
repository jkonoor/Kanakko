-- Refunds, linked and partial (DECISIONS §18).
--
-- §18: "A refund references the transaction it refunds and may be partial...
-- the sum of refunds against a transaction can never exceed it." A refund is
-- negative spending, not income, so it gets its own `type` rather than a
-- negative `amount` — 001's `amount > 0` CHECK stays intact; direction keeps
-- living entirely in `type`, exactly as it already does for
-- expense/income/transfer.
--
-- This is the schema + cross-row guard half of the task, split the same way
-- 010/012 split account_id's nullable-then-enforce pair: the write path
-- (`db.refunds.create_refund`) and the report netting land in this commit
-- too, since a refund row that reports can't see would be a correctness bug,
-- not a stub. The bot-facing `refund <amount>` chooser and the dashboard
-- "Refund" action are a separate task — UX wiring, not money correctness.

-- Widen the type set. 011 named the constraint `transactions_type_check`
-- (001's inline CHECK, Postgres's deterministic `{table}_{column}_check`).
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions
    ADD CONSTRAINT transactions_type_check
    CHECK (type IN ('expense', 'income', 'transfer', 'refund'));

-- The transaction a refund refunds. Nullable because only refunds carry it —
-- same shape as 011's from_account_id/to_account_id.
ALTER TABLE transactions
    ADD COLUMN refund_of_txn_id BIGINT REFERENCES transactions (txn_id);

-- Structural, mirroring 011's transfer-accounts check: the link exists
-- exactly when the type is refund.
ALTER TABLE transactions
    ADD CONSTRAINT transactions_refund_link_check
    CHECK (
        (type = 'refund' AND refund_of_txn_id IS NOT NULL)
        OR (type <> 'refund' AND refund_of_txn_id IS NULL)
    );

-- The cross-row guard: "the sum of refunds against a transaction can never
-- exceed it." No existing guard in this schema aggregates across rows — every
-- prior one is a CHECK or a partial UNIQUE INDEX, both single-row. A trigger
-- is the only way Postgres can express this, so it is the first one here.
-- Runs before the sum guard rejects the write outright, which is why it is
-- BEFORE rather than AFTER: the invariant must hold before the row lands, not
-- be cleaned up after.
CREATE FUNCTION check_refund_does_not_exceed_original() RETURNS TRIGGER AS $$
DECLARE
    original_amount NUMERIC(12, 2);
    refunded_so_far NUMERIC(12, 2);
BEGIN
    SELECT amount INTO original_amount
        FROM transactions WHERE txn_id = NEW.refund_of_txn_id;
    IF original_amount IS NULL THEN
        RAISE EXCEPTION 'refund_of_txn_id % does not exist', NEW.refund_of_txn_id;
    END IF;

    -- `txn_id <> NEW.txn_id` excludes the row being updated from its own sum;
    -- on INSERT, NEW.txn_id is not yet assigned (GENERATED ALWAYS AS
    -- IDENTITY), so the comparison is NULL and excludes nothing extra — the
    -- new row is not in the table yet either way.
    SELECT coalesce(sum(amount), 0) INTO refunded_so_far
        FROM transactions
        WHERE refund_of_txn_id = NEW.refund_of_txn_id
          AND deleted_at IS NULL
          AND txn_id <> NEW.txn_id;

    IF refunded_so_far + NEW.amount > original_amount THEN
        RAISE EXCEPTION
            'refund total % exceeds transaction % (amount %)',
            refunded_so_far + NEW.amount, NEW.refund_of_txn_id, original_amount;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER refunds_do_not_exceed_original
    BEFORE INSERT OR UPDATE ON transactions
    FOR EACH ROW WHEN (NEW.type = 'refund')
    EXECUTE FUNCTION check_refund_does_not_exceed_original();

-- The audit trail (§17) needs a value for this new money mutation, same
-- two-liner as 013.
ALTER TABLE transaction_events DROP CONSTRAINT transaction_events_action_check;
ALTER TABLE transaction_events
    ADD CONSTRAINT transaction_events_action_check
    CHECK (action IN ('confirm', 'undo', 'delete', 'recategorise', 'edit', 'refund'));

-- Recreate the view: `SELECT *` froze the column list, so refund_of_txn_id
-- stays invisible to active_transactions until it is rebuilt (DECISIONS §6).
-- The soft-delete filter is unchanged.
CREATE OR REPLACE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
