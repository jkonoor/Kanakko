-- Audit trail for money mutations (DECISIONS §17).
--
-- Written INSIDE the same transaction as the money change, in db.py, not in the
-- calling handler: a file write cannot be atomic with a database write, but two
-- statements on one connection can, so the audit row can never disagree with the
-- ledger. It answers "who changed this row, when, from what to what?" while the
-- JSON-Lines operational log answers "what happened, and how long did it take?".
--
-- Four operations write one row: confirm, undo, dashboard delete, dashboard
-- recategorise. `before`/`after` are the row's fields as JSONB — NULL on the side
-- that doesn't exist (a confirm has no before, an undo/delete no after).
--
-- `update_id` is NULLABLE: a Mini App mutation (/app/delete, /app/category) is an
-- HTTP POST, not a Telegram update, so it carries no update_id (§17 gap 2).
-- `source` is the field that is always present.
CREATE TABLE transaction_events (
    event_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txn_id     BIGINT      NOT NULL REFERENCES transactions (txn_id),
    user_id    BIGINT      NOT NULL REFERENCES users (user_id),
    action     TEXT        NOT NULL
                   CHECK (action IN ('confirm', 'undo', 'delete', 'recategorise')),
    before     JSONB,
    after      JSONB,
    source     TEXT        NOT NULL CHECK (source IN ('webhook', 'miniapp', 'cron')),
    update_id  BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The audit query: every event for one transaction, oldest first.
CREATE INDEX transaction_events_txn_id_idx ON transaction_events (txn_id, created_at);
