-- Households: the tenant that owns the money (DECISIONS §16).
--
-- §16 moves the tenancy axis: a household owns the money, a user owns their
-- entries. A solo user is a household of one — there is no separate "personal"
-- mode. This migration adds the two tables and migrates every existing user into
-- a household of one; the transactions.household_id backfill is the next task.

CREATE TABLE households (
    household_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The creator, and never NULL: §16 requires a household to always have an
    -- owner, which is why leaving is gated on transferring ownership first.
    owner        BIGINT      NOT NULL REFERENCES users (user_id),
    -- A label with no logic behind it yet (§16): 'beta' keeps today's
    -- testers-on-the-operator's-credits distinguishable from a future free tier.
    plan         TEXT        NOT NULL DEFAULT 'beta',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE household_members (
    household_id BIGINT      NOT NULL REFERENCES households (household_id),
    -- UNIQUE, not merely part of the PK: exactly one household per user (§16). A
    -- second membership row for the same user is a hard error, not a silent
    -- second home — this is the invariant the backfill below relies on.
    user_id      BIGINT      NOT NULL UNIQUE REFERENCES users (user_id),
    joined_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (household_id, user_id)
);

-- The FK migration 004 deferred until this table existed: a household invite's
-- household_id now references a real household. Signup invites carry NULL, which
-- a foreign key permits, so this does not disturb the invites already issued.
ALTER TABLE invites
    ADD CONSTRAINT invites_household_fk
    FOREIGN KEY (household_id) REFERENCES households (household_id);

-- Migrate every existing user into a household of one, owned by themselves.
-- Idempotent: only users without a membership get a household, so a re-run
-- against a partially-migrated database finishes the job rather than minting
-- duplicate households. The UNIQUE on household_members.user_id is the backstop —
-- a second membership for a user would raise, not silently double-home them.
WITH new_households AS (
    INSERT INTO households (owner)
    SELECT u.user_id
    FROM users u
    WHERE NOT EXISTS (
        SELECT 1 FROM household_members m WHERE m.user_id = u.user_id
    )
    RETURNING household_id, owner
)
INSERT INTO household_members (household_id, user_id)
SELECT household_id, owner FROM new_households;
