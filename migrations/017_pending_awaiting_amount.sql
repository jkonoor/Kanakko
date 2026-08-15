-- Track "Change amount" state on a recurring-rule card (DECISIONS §18).
--
-- §18: the cron card offers Confirm / Change amount / Skip. Tapping "Change
-- amount" needs the webhook to know that *this user's next text message* is a
-- replacement amount, not a new transaction to parse — nothing in the schema
-- distinguishes those two cases today. The card being live already implies a
-- row (`pending_transactions`), so the state rides on it rather than a new
-- table: `awaiting_amount` is true from the "Change amount" tap until either a
-- replacement amount is applied (cleared) or the card is cancelled/confirmed
-- (the row is deleted, so the flag goes with it).

ALTER TABLE pending_transactions
    ADD COLUMN awaiting_amount BOOLEAN NOT NULL DEFAULT false;
