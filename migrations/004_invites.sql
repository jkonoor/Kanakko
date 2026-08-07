-- Invite codes: the authorization gate (DECISIONS §16).
--
-- Three grants exist; this table holds two of them. A `signup` invite grants
-- permission to use the bot at all and creates a household of one; a `household`
-- invite grants membership of an existing household and joins nobody new. §16
-- keeps them distinct on purpose — a household invite implies signup, a signup
-- invite puts no one in a household — and the CHECK below makes that structural:
-- exactly the household kind carries a `household_id`.
--
-- Single-use is not a DB constraint but a fact: a code whose `used_by` is set is
-- spent, and the gate refuses it. Codes are labelled (`ravi`, `priya`) so the
-- operator can see who is actually active — attribution a plan column can't give.
CREATE TABLE invites (
    invite_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Arrives as a /start deep-link payload: A-Z a-z 0-9 _ -, up to 64 chars (§16).
    code         TEXT        NOT NULL UNIQUE,
    kind         TEXT        NOT NULL CHECK (kind IN ('signup', 'household')),
    -- No FK yet: the households table lands two tasks later (§16), which ALTERs
    -- this in. NULL for signup, set for household — the CHECK enforces the pairing.
    household_id BIGINT,
    label        TEXT        NOT NULL,
    created_by   BIGINT      NOT NULL REFERENCES users (user_id),
    used_by      BIGINT      REFERENCES users (user_id),
    used_at      TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The §16 invariant, structural: a household invite carries a household, a
    -- signup invite carries none. Without this a mislabelled row silently grants
    -- the wrong thing at the gate.
    CONSTRAINT invites_household_matches_kind
        CHECK ((kind = 'household') = (household_id IS NOT NULL))
);
