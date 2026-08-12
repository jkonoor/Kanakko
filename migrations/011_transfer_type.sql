-- A fourth transaction type: `transfer` (DECISIONS §18).
--
-- §18: money moving between two of a household's own accounts is neither
-- spending nor income — "excluded from every spending and income total". One
-- mechanism answers six features (ATM withdrawal, credit-card bill payment,
-- SIP/FD/chit contribution, opening balance, member-to-member money): each is
-- just a transfer, `from_account` → `to_account`.
--
-- The exclusion needs no report change: `db/reports.py` sums with positive
-- `type = 'expense'` / `type = 'income'` filters, so a `transfer` row is invisible
-- to both totals and to the category breakdown by construction. The guard for
-- that lives in tests/test_migrate.py — a ₹5,000 transfer that moves either total
-- reddens it.

-- Widen the type set. 001 created the check inline, so Postgres named it
-- `transactions_type_check` (its deterministic `{table}_{column}_check` form).
ALTER TABLE transactions DROP CONSTRAINT transactions_type_check;
ALTER TABLE transactions
    ADD CONSTRAINT transactions_type_check
    CHECK (type IN ('expense', 'income', 'transfer'));

-- A transfer names both ends; every other type names neither. The endpoints are
-- accounts (§18), nullable because only transfers carry them.
ALTER TABLE transactions
    ADD COLUMN from_account_id BIGINT REFERENCES accounts (account_id),
    ADD COLUMN to_account_id   BIGINT REFERENCES accounts (account_id);

-- Structural, not hoped-for: the endpoints exist exactly when the type is
-- transfer. A transfer missing an end, or an expense that smuggled one in, is a
-- hard error — the invariant §18 rests on can't be violated by a stray write.
ALTER TABLE transactions
    ADD CONSTRAINT transactions_transfer_accounts_check
    CHECK (
        (type = 'transfer'
            AND from_account_id IS NOT NULL AND to_account_id IS NOT NULL)
        OR (type <> 'transfer'
            AND from_account_id IS NULL AND to_account_id IS NULL)
    );

-- Recreate the view: `SELECT *` froze the column list, so the two new columns
-- stay invisible to active_transactions until it is rebuilt (DECISIONS §6). The
-- soft-delete filter is unchanged.
CREATE OR REPLACE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
