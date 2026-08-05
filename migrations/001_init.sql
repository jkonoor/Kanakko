-- Initial schema: users, transactions, pending_transactions, reminder_log,
-- and the active_transactions view. Data model per docs/PLAN.md.

CREATE TABLE users (
    user_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_user_id BIGINT      NOT NULL UNIQUE,
    timezone         TEXT        NOT NULL DEFAULT 'Asia/Kolkata',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
    txn_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT        NOT NULL REFERENCES users (user_id),
    -- NUMERIC, never float: a ledger that drifts by paise is worse than none
    -- (DECISIONS §9). Direction lives in `type`, so the amount is positive.
    amount      NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    type        TEXT          NOT NULL CHECK (type IN ('expense', 'income')),
    -- Categories are a closed set owned by kanakko/categories.py (DECISIONS
    -- §11), so no CHECK here — the DB would be a second place to edit them.
    -- NULL means the model could not tell; the bot then asks (DECISIONS §3).
    category    TEXT,
    note        TEXT,
    -- When the money moved, not when it was logged — reports bucket on this.
    occurred_on DATE          NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);

-- The report query: one user's live rows over a date range.
CREATE INDEX transactions_user_occurred_on_idx
    ON transactions (user_id, occurred_on)
    WHERE deleted_at IS NULL;

-- Parsed but unconfirmed; a row lives from the confirm card to Confirm/Cancel.
CREATE TABLE pending_transactions (
    pending_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             BIGINT      NOT NULL REFERENCES users (user_id),
    telegram_message_id BIGINT      NOT NULL,
    parsed              JSONB       NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What was sent when; the noon nudge reads this to decide whether to stay
-- silent (DECISIONS §12).
CREATE TABLE reminder_log (
    reminder_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users (user_id),
    kind        TEXT        NOT NULL CHECK (kind IN ('noon', 'evening', 'monthly')),
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX reminder_log_user_kind_sent_at_idx
    ON reminder_log (user_id, kind, sent_at DESC);

-- Every read goes through this view, never `transactions` (DECISIONS §6) —
-- one forgotten WHERE clause would otherwise resurrect deleted rows in a total.
CREATE VIEW active_transactions AS
    SELECT * FROM transactions WHERE deleted_at IS NULL;
