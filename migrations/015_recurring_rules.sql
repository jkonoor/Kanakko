-- Recurring rules for auto-debits (DECISIONS §18).
--
-- §18: "A recurring rule (amount, category, account, day of month, active) is
-- the answer to auto-debits... On the day, the cron sends the ordinary confirm
-- card... Not a silent insert." This migration adds the table and its
-- constraints, plus kanakko.db.recurring — the full CRUD (create/list/pause/
-- delete) the cron and the dashboard's pause/delete will call. Split the same
-- way 014 split refunds: this is the schema and the household-scoped write
-- path, not a stub — a rule created here is a real row a future task can act
-- on. The cron that finds a rule due today and sends the confirm card (reusing
-- §4's machinery) and the dashboard's pause/delete controls are separate
-- tasks: they need jobs/ scheduling and Confirm/Change-amount/Skip wiring that
-- would make this one commit two unrelated things.
--
-- No transaction row references a rule yet — the cron task decides how a rule
-- ties to the transaction it eventually creates (e.g. a nullable
-- recurring_rule_id column) — so a rule can be deleted outright with no
-- orphaning to worry about; there is nothing yet that could point at it.

CREATE TABLE recurring_rules (
    rule_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Household-scoped like accounts (§18) — §16's shared ledger means any
    -- member may set up, pause or remove a rule, not just the one who made it.
    household_id BIGINT         NOT NULL REFERENCES households (household_id),
    account_id   BIGINT         NOT NULL REFERENCES accounts (account_id),
    -- No CHECK — categories.py is the one place category strings are declared
    -- (CLAUDE.md), the same reason transactions.category carries none.
    category     TEXT           NOT NULL,
    amount       NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    day_of_month SMALLINT       NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    active       BOOLEAN        NOT NULL DEFAULT true,
    created_by   BIGINT         NOT NULL REFERENCES users (user_id),
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT now()
);

-- The cron's daily lookup: today's active rules, across every household.
CREATE INDEX recurring_rules_due_idx
    ON recurring_rules (day_of_month)
    WHERE active;

-- A household's own rules, for the dashboard's pause/delete list.
CREATE INDEX recurring_rules_household_idx
    ON recurring_rules (household_id);
