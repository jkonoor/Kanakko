-- Per-user daily message cap (DECISIONS §16, "Cost control").
--
-- Every inbound message is one LLM call (§2), so an unbounded user is an
-- unbounded bill paid by the household owner. §16 decided a per-user daily cap,
-- counted off `processed_updates` — the table that already records one row per
-- handled update (migration 002) — rather than a new counter table. It carried
-- only `update_id`; this adds the acting user so the webhook can count a user's
-- handled updates per IST day before the LLM call and refuse past the cap.
--
-- Nullable: rows written before this migration have no user, and they are older
-- than any day the cap counts (it only ever looks at today's IST rows), so a NULL
-- there never affects a count. New claims always carry the user.
ALTER TABLE processed_updates
    ADD COLUMN user_id BIGINT REFERENCES users (user_id);
