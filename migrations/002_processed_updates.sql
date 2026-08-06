-- Idempotency ledger for Telegram redelivery (DECISIONS §14).
--
-- Telegram redelivers any update it did not get a 2xx for. Recording each
-- update_id lets the webhook make every handler idempotent against redelivery
-- in ONE place, rather than per-handler. It matters most on the money path:
-- `/undo` soft-deletes "the newest live row" with no per-message anchor (unlike
-- Confirm/Cancel, which key on the card's message id), so a redelivered `/undo`
-- would otherwise soft-delete a *second* real transaction and silently drop it
-- from every total. The webhook claims the update_id in the same transaction as
-- the handler, so the claim commits with the handler's writes and rolls back
-- with them — a handler that 500s is legitimately retried.
--
-- ponytail: grows one row per update forever; a personal-scale ledger, so no
-- pruning. Add a retention sweep (delete rows older than a few days) if it ever
-- matters — the id is monotonically increasing, so old rows never repeat.
CREATE TABLE processed_updates (
    update_id    BIGINT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
