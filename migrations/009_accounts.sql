-- Accounts: money has a location (DECISIONS §18).
--
-- §18: a transaction had a type but no place. An account is a named pool with a
-- kind and an opening balance — the one mechanism that answers savings, credit
-- cards, family transfers and reconciliation at once. This migration adds the
-- table and its constraints only; the transaction column, the transfer type and
-- every report change land in the tasks that follow. No transaction row changes
-- here.

CREATE TABLE accounts (
    account_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Accounts belong to the household (§18) — §16's shared ledger means everyone
    -- sees everything, so no new visibility rule is needed.
    household_id    BIGINT        NOT NULL REFERENCES households (household_id),
    -- Each account names an owning member (§18). For the backfilled `external`
    -- account below this is the household owner.
    owner           BIGINT        NOT NULL REFERENCES users (user_id),
    -- The four kinds cover everything §18 works through. UPI is deliberately not
    -- here: it is a rail that pulls from a `spending` account, not a pool.
    kind            TEXT          NOT NULL CHECK (kind IN ('spending', 'credit', 'locked', 'external')),
    name            TEXT          NOT NULL,
    -- Signed, and never float (§9): a `credit` account's opening balance is what
    -- is *owed*, so it can be negative. NUMERIC, so it cannot drift by paise.
    opening_balance NUMERIC(12, 2) NOT NULL DEFAULT 0,
    -- Where a transaction that names no account lands (§18). At most one live
    -- default per household — enforced by the partial index below, not app code.
    is_default      BOOLEAN       NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

-- One default per household, enforced structurally (§18). Partial on both
-- predicates: a household may only have one *live* default, but a soft-deleted
-- former default must not block naming a new one.
CREATE UNIQUE INDEX accounts_one_default_per_household
    ON accounts (household_id)
    WHERE is_default AND deleted_at IS NULL;

-- The account lookup for a household: its live accounts.
CREATE INDEX accounts_household_idx
    ON accounts (household_id)
    WHERE deleted_at IS NULL;

-- Every household gets one `external` account — §18 makes it the counterparty
-- for opening balances and adjustments, so it is structural, not optional.
-- Existing households (created before this migration) get theirs here, owned by
-- the household owner. Idempotent: a re-run skips households that already have
-- one, so a retried migration finishes rather than minting duplicates.
INSERT INTO accounts (household_id, owner, kind, name)
SELECT h.household_id, h.owner, 'external', 'External'
FROM households h
WHERE NOT EXISTS (
    SELECT 1 FROM accounts a
    WHERE a.household_id = h.household_id AND a.kind = 'external'
);
