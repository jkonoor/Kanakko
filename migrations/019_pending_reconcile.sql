-- Track "awaiting a reconcile figure" state (DECISIONS §18).
--
-- §18: the weekly nudge asks "what does your bank say?", and the *next* text
-- message from that user is the answer, not a new transaction to parse — the
-- same distinction 017's `awaiting_amount` drew for "Change amount". Unlike a
-- "Change amount" reply, a reconcile nudge answers no `Transaction` at all —
-- there is nothing parsed to hold the state on — so `parsed` becomes nullable
-- and `awaiting_reconcile_account_id` names which account the reply answers
-- for. A CHECK keeps the two kinds of pending row from blurring: exactly one
-- of "an ordinary pending transaction" (`parsed` set) or "an awaiting
-- reconcile reply" (`awaiting_reconcile_account_id` set) holds per row.

ALTER TABLE pending_transactions
    ALTER COLUMN parsed DROP NOT NULL,
    ADD COLUMN awaiting_reconcile_account_id BIGINT REFERENCES accounts (account_id);

ALTER TABLE pending_transactions
    ADD CONSTRAINT pending_transactions_parsed_xor_reconcile_check
    CHECK ((parsed IS NOT NULL) <> (awaiting_reconcile_account_id IS NOT NULL));
