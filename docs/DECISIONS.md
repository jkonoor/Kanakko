# Design decisions

Outcome of the design interview, 2026-08-05. Each entry records what was
chosen, why, and what was rejected — the rejected options matter, because
several of them are the obvious-looking choice.

---

## 1. Single user, but never single-tenant by accident

**Decided:** Build for one user. No auth, signup, billing, or session code.
Every table carries a `user_id` column from day one, populated with a constant.

**Why:** "Load scalability" is not a real constraint here — 10,000 users
logging 5 transactions/day is ~0.6 writes/second, which one Postgres instance
handles without noticing. "Tenancy scalability" *is* real, because retrofitting
it is a rewrite. A `user_id` column costs nothing now and makes the multi-user
change small later; auth and signup are the expensive parts and are pure
speculation until a second person asks to use it.

**Rejected:** building multi-user now (weeks of work for a hypothetical user);
omitting `user_id` (guarantees a schema migration later).

---

## 2. LLM parse on every message, via OpenRouter

**Decided:** One LLM call per inbound message. Provider is **OpenRouter** so the
model is a config value. Default model **`google/gemini-2.5-flash`** (revised
2026-08-07 — see below; was `claude-opus-5`). Request structured output via
`response_format: {type: "json_schema", ...}` with `require_parameters: true` in
provider preferences.

**Why:** Cost is not a constraint at this scale. Verified first-party rates
(cached 2026-06-24): Haiku 4.5 $1/$5 per MTok, Sonnet 5 $3/$15, Opus 5 $5/$25.
A parse is ~300 input + ~60 output tokens, so roughly ₹0.05–₹0.25 per
transaction — under ₹40/month at personal volume. Opus 5 was chosen originally
for extraction accuracy, on the reasoning that cost only starts mattering around
~1,000 users and that switching is exactly what OpenRouter makes cheap.

**Revised 2026-08-07 — the switch was made early, on measurement rather than on
the user threshold.** Once the parse path actually worked in production (it never
had — see the schema caveat below), the models were compared on this app's real
workload instead of assumed: 8 extraction cases plus 2 messages that must *not*
parse, run against the live API, three times over for the shortlist.

| Model | Correct | Invented a transaction | ~sec/parse | Per parse |
|---|---|---|---|---|
| `claude-opus-5` | 8/8 | 0 | 5.2 | $0.0040 |
| `claude-sonnet-5` | 8/8 | 0 | 5.1 | $0.0016 |
| **`google/gemini-2.5-flash`** | **8/8 ×3** | **0** | **1.6** | **$0.00032** |
| `openai/gpt-5-nano` | 8/8 | 0 | 9.6 | $0.000052 |
| `mistralai/mistral-small-24b` | 7/8 ×3 | 0 | 2.2 | $0.000026 |
| `google/gemini-2.5-flash-lite` | 7/8 ×3 | **2 of 3 runs** | 1.4 | $0.000072 |

Gemini 2.5 Flash matched Opus perfectly across three runs while being ~3× faster
and ~12.5× cheaper (≈₹4/month at 5 transactions/day). Latency is the part that
shows: the user waits on this call before a confirm card appears.

**The must-not-parse cases are the important half of that table**, and any future
comparison keeps them. `gemini-2.5-flash-lite` fabricated a transaction from
`"hello how are you"` — ₹150, Entertainment — in two of three runs. In a ledger
whose whole value is being trusted, inventing money is disqualifying at any
price, and a pure accuracy score would have ranked it acceptable. The cheaper
models that did *not* invent instead failed relative dates ("last friday"), which
is a quieter wrongness that §10's prompt-injected date exists to prevent.

**Unchanged:** the model stays a config value (`OPENROUTER_MODEL`), so this is a
default and not a lock-in. Revisit on the same evidence — a table, not a vibe.

**Rejected:**
- *Regex/rules with LLM fallback.* Looks thrifty; is the classic three-rewrite
  trap. `spent 500 groceries` → `500 for groceries` → `groceries 500` →
  `paid ₹500 to bigbasket` → each a new pattern, forever, to save ₹0.05.
- *Pure rules with a fixed syntax.* Defeats the premise — a syntax the user must
  remember *is* the discipline the project exists to remove.

**Caveat that must be built for:** OpenRouter's structured-output support varies
by model **and** provider, and per its docs *"strict mode enforcement varies by
provider; some guarantee schema compliance while others treat it as guidance."*
So the schema is **not** guaranteed the way it is on a first-party API. Keep one
Pydantic validation plus a single retry. This is the actual price of the
provider-abstraction layer, and it is small.

---

## 3. Uncertainty is expressed by nullable fields, not a confidence score

**Decided:** Fields the model genuinely cannot determine come back `null`.
`category: null` → show the category buttons. No confidence score anywhere.
`amount` is **not** nullable — no amount means no transaction, so reject the
message and ask for a rephrase rather than showing a confirm card with a blank
in the only field that matters.

**Why:** The failure mode of an LLM is being confidently wrong, not visibly
unsure. A nullable field asks the model a question it can answer ("can you
tell?"); a confidence score asks one it can't ("how sure are you, numerically?").
Self-reported LLM confidence is poorly calibrated, so thresholding it means
tuning a constant against vibes forever. The nullable enum is also enforced by
the JSON schema rather than by validation code.

---

## 4. Confirm card on every transaction, always

**Decided:** Every parsed transaction shows a confirmation before it is stored.
No "high confidence, skip confirmation" fast path — not now, not later.

**Why:** The confirm step is what makes fast, sloppy natural-language entry
*safe*. It reduces a parsing mistake from a corrupted ledger to one tap. A
skip-confirmation path is exactly where accuracy problems would hide.

---

## 5. No field editor — three cheaper things instead

**Decided:** Replace the `Edit` button with:

1. **Category buttons directly on the confirm card** — category is the
   most-often-wrong field and is a closed set, so it's already one tap.
2. **Wrong amount or date → Cancel and retype.** Re-entry takes ~3 seconds via
   the natural-language path that already exists.
3. **`/undo`** for the most recent confirmed transaction.
4. **The Mini App transaction list** for anything older than the last entry.

**Why:** A field editor means a multi-step conversation state machine —
per-user "waiting for amount" state, abandonment handling, out-of-order
messages. In a Telegram bot that is usually the single largest source of bugs.
A field-by-field editor is also a second, worse input method for data the
natural-language path already accepts.

**This is a deliberate narrowing of the original spec**, which said
Confirm · Edit · Cancel. Chosen knowingly, not by omission.

---

## 6. Soft delete

**Decided:** `deleted_at TIMESTAMPTZ NULL`. All reads go through a view:

```sql
CREATE VIEW active_transactions AS
  SELECT * FROM transactions WHERE deleted_at IS NULL;
```

**Why:** This is the data-loss axis, where shortcuts aren't taken. An
accidental hard delete of a real transaction is unrecoverable in a ledger the
user is meant to trust. One column and one view is the cheapest possible
insurance, and it makes `/undo` itself reversible. The view means the filter
lives in exactly one place — a single forgotten `WHERE` clause can't resurrect
deleted rows inside a monthly total.

---

## 7. Stack: Python · FastAPI · PostgreSQL · plain SQL

**Decided:** Python, FastAPI, `python-telegram-bot`, PostgreSQL, `psycopg` with
hand-written SQL, numbered `.sql` migration files.

**Why:** Python because the Telegram libraries are mature there and it's the
author's daily language — don't learn a stack to build a money tracker. FastAPI
because an HTTP server is needed anyway for the Telegram webhook and the Mini
App, and one process serves both.

**Postgres over SQLite** is the one place extra effort was spent: SQLite would
be lazier (one file, zero ops, ample for this write volume), but a
SQLite→Postgres migration is small in theory and a weekend in practice — type
coercion, concurrent writes, pooling. Postgres costs one container now and
deletes that migration entirely. Same logic as the `user_id` column.

**No ORM.** Four tables and a handful of `GROUP BY` aggregates is exactly the
shape where an ORM adds indirection and you drop to raw SQL for the reports
anyway. Adopt SQLAlchemy + Alembic if the schema starts churning weekly.

---

## 8. No Celery, no Redis

**Decided:** Neither. Scheduling is a **cron sidecar container** in the compose
stack (same image, different command).

**Why:** Celery + Redis is a distributed task queue built for fan-out, retries,
and worker pools. The actual requirement is three fixed-time messages a day.
That's three crontab lines versus three new moving parts (broker, worker, beat).
Redis has nothing to cache — the hot query is "sum one user's transactions this
month", an indexed scan over a few hundred rows.

**Why a sidecar rather than host cron or an in-process scheduler:**

- **Host cron** is out: the Dokploy host is team-owned, and a personal project
  shouldn't be adding root crontab entries to it.
- **Dokploy's own scheduler** is out: its cron support is database-backup-only
  (`backup.create`, run by `node-schedule` inside Dokploy). There is no
  general-purpose "run this command on a schedule" feature.
- **APScheduler in-process** was the runner-up — one dependency, no extra
  container — but it lives in the app process, so the day a second replica runs,
  every reminder fires twice. A sidecar keeps scheduling outside the app and has
  no such hazard. Acceptable fallback if running a second container proves
  awkward.

---

## 9. Money is `NUMERIC`, never float

**Decided:** `NUMERIC(12,2)` in Postgres, `Decimal` in Python. One
assert-based check covering parse → store → sum-by-category.

**Why:** `0.1 + 0.2 != 0.3` in binary floating point. A ledger that drifts by
paise is worse than no ledger, because it is trusted. Non-negotiable.

---

## 10. Time: UTC storage, `Asia/Kolkata` semantics

**Decided:** All timestamps `timestamptz` stored in UTC. Every day/month
bucketing done as `... AT TIME ZONE 'Asia/Kolkata'`. Cron runs in that zone.
The current date in `Asia/Kolkata` is injected into **every** LLM prompt.

**Why:** Three scheduled jobs and two reporting boundaries ("today's total",
"previous month") all depend on when a day begins. A UTC server without an
explicit decision gives you: the "evening" reminder at 2:30 AM, the monthly
summary cutting the month at 05:30 IST so the last 5.5 hours of the 31st land in
the wrong month, and "today" resetting mid-afternoon. All silent — nothing
errors, the numbers are just quietly wrong.

The prompt injection matters separately: the model has no clock, so without
today's date it cannot resolve "yesterday" or "last Friday" and will guess.

**For multi-user:** read the timezone through one function that currently
returns the constant and later returns a per-user column. Call sites don't
change.

**Rejected:** storing local time "because it's easier to read" — looks fine in
a DST-free country and breaks permanently the moment a second timezone exists.

---

## 11. Closed category set, defined once

**Decided:** A closed enum, declared in one Python constant that generates both
the JSON-schema `enum` and the Telegram keyboard.

Expenses: `Food` · `Groceries` · `Transport` · `Shopping` ·
`Bills & Utilities` · `Health` · `Entertainment` · `Other`

Income: `Salary` · `Freelance` · `Refund` · `Other`

**Why:** Free text destroys the feature it appears to enable — `Food`, `food`,
`Groceries`, `Food & Dining` and `Eating out` become five rows in a spending
breakdown whose entire purpose is grouping. Normalizing them afterwards is a
fuzzy-matching problem nobody wants to own. A closed set also lets the schema
`enum` make it *impossible* for the model to invent a category.

Groceries was split out of the original five because the spec's own example
(`Spent ₹500 on groceries`) was ambiguous between `Food` and `Shopping` — the
model would pick inconsistently and trend data would wobble for no real reason.

`Other` plus a free-text `note` preserving the original wording is the escape
hatch, so nothing is lost and nothing is misfiled.

**Deferred:** user-defined categories. Repeatedly forcing things into `Other`
is the signal to add one — and it names which one.

---

## 12. Three scheduled jobs

| Job | Time (`Asia/Kolkata`) | Behaviour |
|---|---|---|
| Noon nudge | 12:00 daily | **Suppressed** if anything was logged since the previous evening summary |
| Evening summary | 21:00 daily | **Unconditional.** Carries the day's total and entry count |
| Monthly report | 09:00 on the 1st | Previous month: income, expenses, balance, top categories |

**Why the noon suppression:** the bot cannot know whether unrecorded expenses
exist — it only knows what it was told. An unconditional nudge is therefore
wrong much of the time, and a notification that's wrong half the time trains you
to mute it, which returns you to the original problem. One `SELECT` before
sending means it only ever arrives when it's actually right.

**Why the evening one is unconditional:** it carries the day's total, so it
gives you something even on days you don't act on it. It doubles as the daily
summary rather than being purely an interruption.

---

## 13. Dashboard: Telegram Mini App

**Decided:** A Telegram Mini App, opened from a button in the chat.
Authentication by validating `initData`.

Verified mechanism (Telegram Mini Apps docs): `initData` is signed with
`HMAC-SHA256`, where the secret key is `HMAC-SHA256(bot_token, "WebAppData")`.
The backend sorts the received fields into a data-check string, recomputes the
hash, and compares. A match proves the payload came from Telegram — including
the user's ID.

**Why:** This is a native platform feature beating custom code. About fifteen
lines of standard-library `hmac`/`hashlib` **permanently delete authentication
from the roadmap** — no login form, sessions, password reset, or OAuth, ever.
That's normally one of the most expensive parts of turning a side project into a
SaaS.

**Rejected:**
- *Server-rendered page at a secret URL.* Looks lazier, isn't. The secret leaks
  through browser history, screenshots and link previews, and you still build
  the login you skipped.
- *SPA with real auth.* Over-built for a page rendering six numbers and a list.

**Contents:** totals, balance, category breakdown, weekly summary, monthly
summary, and a recent-transactions list with per-row delete. That list is a
small addition beyond the original spec, needed to satisfy correcting older
entries (see §5).

**Charts: none, initially.** Spending across 6–8 categories reads better as a
sorted list with CSS percentage bars than as a pie chart — more legible, more
accessible, works in both themes, zero dependencies. A weekly trend is a row of
bars. Add a charting library when something genuinely needs one, not to draw six
rectangles.

---

## 14. Hosting: a shared, self-hosted Dokploy instance

**Decided:** Deploy to `<dokploy-host>` (shared Dokploy instance),
with permission. Telegram **webhook** rather than long polling, since HTTPS is
available there anyway and the Mini App requires it regardless.

**Why not a free PaaS** (investigated and rejected on verified facts):

- **Render free tier** fails three ways: web services spin down after 15 minutes
  idle and take ~1 minute to wake (you'd wait a minute for a confirm card); free
  Postgres **expires 30 days after creation** and is deleted after a 14-day
  grace period; Cron Jobs are not in the free tier. Also 750 instance-hours per
  workspace per month, under a full month of always-on.
- **Fly.io** no longer offers plans to new customers as of 2026; free allowances
  survive only for grandfathered organizations. Smallest always-on machine
  ≈ $2.02/month, Postgres extra.

This is not bad luck: an always-on endpoint, a database that isn't deleted, and
scheduled jobs are precisely what free tiers remove. A finance ledger on a
database with a 30-day expiry is not a cost saving.

**Open governance item:** this is personal financial data on employer
infrastructure, and the stated intent is for it to become a commercial product.
Access permission was granted; data ownership and IP for a SaaS built and hosted
on company resources is a separate question worth settling in writing early.
Does not affect the architecture.

---

## 15. The webhook verifies its origin, and fails closed

**Decided:** `/webhook` requires Telegram's `secret_token`.

- The secret is registered with Telegram on `setWebhook` and arrives on every
  request in the **`X-Telegram-Bot-Api-Secret-Token`** header (verified against
  the Bot API docs, 2026-08-06).
- Env key **`TELEGRAM_WEBHOOK_SECRET`**. **Required**, not optional — compose
  declares it with `:?` like the other keys, and the endpoint returns **403**
  when the header is absent or does not match.
- **Fails closed:** if the secret is unset, every request is rejected. An unset
  secret must never mean "accept everything" — that is the exact failure that
  looks fine in development and silently ships an open endpoint.
- Compared with `hmac.compare_digest`, not `==`, same as `initData` (§13).
- Telegram's allowed alphabet is narrower than URL-safe base64: **`A-Za-z0-9_-`
  only, 1–256 characters.** A generator using `.` or `~` produces a token
  `setWebhook` rejects.

**Why this needs to exist at all:** §1 decided no user-facing auth and §13
solved the dashboard with `initData`, but neither covers the webhook, and §14
only chose webhook over long polling. The endpoint is a public URL. Once the
Confirm handler writes rows, an unauthenticated POST forges a transaction in
the ledger — and a finance tracker whose numbers can be written by strangers is
worse than no tracker, because it is trusted.

**Why not an allowlist of Telegram's IP ranges:** it is a second thing to keep
current, it breaks when Telegram changes ranges, and it does not authenticate —
it only narrows. The secret is one header comparison and Telegram's own
documented mechanism.

**Rejected:** accepting unauthenticated updates until the bot goes live. The
handler that makes it dangerous is the next task in the queue, and "we'll add
auth before launch" is how it ships without.

---

## 16. Households: multi-tenant, shared ledgers, invite-gated

Decided 2026-08-07. §1 deferred multi-tenancy until "a second user exists" — that
trigger has now fired, and the intent has widened from a personal tool to
something distributed to testers and then sold.

**Already true, by accident.** §1's "carry `user_id` from day one" worked so well
that the bot is *already* multi-user: no id is hardcoded, `get_or_create_user`
mints a row for whoever messages, every query is scoped, and the jobs fan out over
`all_users`. Anyone who finds the bot today gets a working private ledger — parsed
on the owner's OpenRouter credits. **That is the gap this section closes: not
authentication, but authorization.**

### The household is the tenant

**Decided:** A **household** owns the money; a **user** owns their entries. A solo
user is a household of one — there is no second, "personal" mode.

**Why:** the alternative is dual-mode, where a user belongs to both a personal and
a shared ledger, and every inbound message needs a "which ledger?" answer. That
destroys the one-tap flow §4 and §5 exist to protect. One household per user keeps
a single code path: a message always has exactly one destination.

Transactions therefore carry two axes, not one: **which household the money
belongs to**, and **which member entered it**. §1's `user_id` column prepared the
first axis only; the second is new, and every read gains a household scope.

### Rules

| Rule | Decision |
|---|---|
| Households per user | Exactly one |
| Owner | The creator |
| `/undo` | Removes **your own** last entry, never the household's |
| Edit / delete a row | Only the member who entered it |
| Remove a member | The owner may remove anyone; **any member may remove themselves** |
| Owner leaving | **Must transfer ownership first** — a household always has an owner |
| On removal | Ask **retain or delete** that member's entries, with a warning |
| Noon nudge | Suppressed **per person**, not household-wide |
| Evening / monthly summary | Household figures, sent to every member |
| Private expenses | **Not on the roadmap** |
| Billing | Attaches to the **household**; the owner pays |

**Why `/undo` is personal while the ledger is shared:** undo is "fix what I just
typed", and the command carries no per-message anchor (§14). Household-wide undo
would let a reflexive `/undo` silently delete a partner's entry. "You own your
entries, the household owns the money" is one sentence a user can hold in their
head, and it settles the edit and delete rules too.

**Why a member may remove themselves** even though removal is otherwise the
owner's: owner-only removal traps a person in a ledger they cannot leave, with
their entries still flowing into it. Tolerable in a family, untenable in a product
someone pays for. Owner-removes-others stays owner-only; the self case is the same
code path with the actor and the target equal.

**Deletion is not `/undo`.** §6's soft delete stays exactly as it is — recoverable,
the whole point of the ledger being trustworthy. Member deletion is a *separate,
irreversible* operation and must never share a name with it. It carries a specific
warning, accepted knowingly: **hard-deleting a departed member's entries makes
past reports stop reconciling.** An evening summary that said ₹18,920 at the time
will not match that month re-opened later. That is the honest price of real
deletion, and the warning says so rather than only "this cannot be undone".

### Authorization, not authentication

**Decided:** No password, no login, no session — ever. Telegram is the identity
provider, as §13 established. Two proofs already exist: the webhook's
`secret_token` (§15) proves an update came from Telegram, and the Mini App's
`initData` HMAC (§13) proves which user is asking. What was missing is a single
check — *is this Telegram user permitted?* — before any work is done.

**Three separate grants, deliberately not conflated:**

| | Grants | Creates | Issued by |
|---|---|---|---|
| **Signup invite** | Permission to use the bot at all | A household of one | The operator |
| **Household invite** | Membership of an existing household | Nothing | That household's owner |
| **Coupon** | A discount on a paid plan | Nothing | Later, with billing |

A household invite implies signup permission; a signup invite does not put anyone
in a household. Codes are **single-use and labelled** (`ravi`, `priya`) so an
operator can tell who is actually active during testing — attribution a plan
column cannot give.

Both arrive by deep link. **Verified** (`core.telegram.org/bots/features`,
2026-08-07): a `start` payload is **up to 64 characters** from `A-Z a-z 0-9 _ -`,
base64url recommended, and `https://t.me/<bot>?start=<code>` delivers `/start
<code>`. A prefix plus a random token fits comfortably.

**`SIGNUP_MODE` is an env var** (`invite` | `open`), alongside `OPENROUTER_MODEL`
and `TELEGRAM_WEBHOOK_SECRET`. Going from closed beta to public signup is one
environment change — the policy is config, the mechanism is permanent. In
`invite` mode an unrecognised user gets a polite refusal and **nothing is
stored**, not even a user row.

**`ADMIN_TELEGRAM_IDS` names the operator** — comma-separated Telegram user ids,
and the answer to "issued by the operator" in the table above. `/invite_signup
<label>` is gated on it; `/invite <label>` stays gated on owning a household,
which the `households` table can answer in SQL. The operator is deliberately
**not** a database role: a signup invite hands out the bot itself, so deriving the
right from anything a user can acquire in-band — owning a household, being the
first row — makes a closed beta that opens itself, one tester at a time. Fails
closed like `SIGNUP_MODE`: unset admits nobody, and a non-integer entry is skipped
rather than crashing the webhook.

The command is `/invite_signup`, an underscore rather than a hyphen, because
Telegram recognises only `a-z 0-9 _` in a command: `/invite-signup` would split at
the hyphen, so BotFather could not register it and the client would not render it
as tappable — a command that works only for whoever already knows to type it.

**A known bug this exposes:** `handle_text` uses `msg.chat_id` as identity and
never reads `message.from.id`. In a private chat the two coincide, so it works
today; with households it is wrong. Identity and delivery address are different
things and must be separated **before** the schema change, not after.

### Cost control

**Decided:** a per-user daily message cap, **default 50, set by env var** — never
hardcoded. Every inbound message is one LLM call (§2), so an unbounded user is an
unbounded bill paid by the household owner.

`processed_updates` (migration 002) already records one row per handled update but
carries only `update_id`. Adding `user_id` gives a per-user, per-IST-day count off
a table that already exists, counting exactly the thing that costs money.

### Plans

**Decided:** `households.plan`, defaulting to **`'beta'`** — a column with no logic
behind it yet.

**Why `'beta'` and not `'free'`:** these testers arrived on the operator's credits
during testing. Labelling them `free` makes them indistinguishable later from a
free-tier user who signed up in year two, and those two deserve different
treatment. The column costs nothing now and keeps the question answerable.

**Rejected:** a free plan *instead of* invite codes. A plan describes entitlement,
never admission — with open signup, a stranger still costs the operator money on
the free tier. The gate and the plan are orthogonal.

### Open questions — research, not opinion

- **Telegram's native Payments API** — does it support recurring subscriptions for
  an Indian seller? Worth answering before reaching for Razorpay or Stripe: the
  same native-platform instinct that made `initData` delete login from the roadmap
  (§13) may apply again. **UNVERIFIED** — nobody has checked.
- **Data protection.** Holding *other people's* financial data raises obligations
  a personal tool never had, sharpened by §14's still-unsettled point that this
  runs on employer infrastructure. What Indian law requires here is a question to
  answer before real users, not to assume. **UNVERIFIED.**

---

## 17. Logging: one call, two sinks, a trace mode

Decided 2026-08-07, after auditing the codebase and studying the pipeline-debug
logger in the `ha-backend` project.

**The problem.** Six log calls existed in the whole application, five of them the
jobs' one-liners. **Every money-mutating path logged nothing** — confirm, cancel,
`/undo`, category change, dashboard delete. A user saying "my total is wrong" left
nothing to reconstruct. That was tolerable while one person used it and remembered
what they did; §16 makes it other people's money.

### The idea worth stealing, and the one worth leaving

`ha-backend` writes one JSON file per operation into a per-document folder, with
the outcome **in the filename** — `fn__save_result_to_supabase__error.json`. Its
strength is that `ls` is the summary: an agent lists a folder, sees what failed,
and opens one small file, rather than parsing an interleaved stream. Numeric
prefixes make alphabetical order chronological. Secrets are scrubbed, writes are
wrapped so logging can never break processing, and folders rotate.

**Adopted:** the outcome in the identifier, one call on *both* the success and
error path, never throwing, a scrubber plus an explicit never-log list, a duration
on every event, and a correlation id.

**Not adopted:** the ~30 hand-written typed methods (`logOcrInput`,
`logDocumentTypeOutput`, …). `ha-backend`'s own folders no longer match the layout
documented at the top of that service — they are full of generic `fn__`/`step__`
files, because the codebase converged on a single generic `logStep`. Copy what a
project converged *on*, not what it converged *away from*.

**Why the shape still differs here.** A document there is 30 steps over ~40
seconds and has a story worth reconstructing; a Kanakko message is ~4 steps over
~2 seconds and has almost none. So the folder-per-entity form is kept for the
trace mode, and ordinary operation logs one line per event.

### Two sinks, because there are two needs

| | Operational log | Audit trail |
|---|---|---|
| Answers | "what happened, and how long did it take?" | "who changed this row, when, from what to what?" |
| Store | JSON Lines on a mounted volume | Postgres |
| Retention | rotated | permanent |

**The audit trail is in Postgres and not on the volume, deliberately.** The audit
row is written **in the same transaction as the money change**, so it cannot
disagree with the ledger. A file write cannot be atomic with a database write: a
process death between the two leaves an audit that is wrong, which is worse than
one that is absent. It is also queryable, and it rides the existing backup.

**And it is written inside the same `conn.transaction()` block as the money
statement — in `db.py`, not in the calling handler.** Decided 2026-08-07, walking
the money paths before writing any of this. A handler-level write is atomic
*today*, but only because a prior statement happens to have opened the
transaction, which makes `db.py`'s inner `conn.transaction()` a savepoint rather
than a top-level commit. Change how the user is resolved — §16's identity fix does
exactly that — and the money row commits when the db function returns, leaving the
handler's audit write outside it: a confirmed transaction with no record of who
created it. The failure is silent, production-only, and a test written today would
stay green through it. Physical adjacency to the money statement is the only form
of this that cannot come apart. The price, accepted: the money functions take the
acting user and the correlation id as arguments rather than the handler supplying
them afterwards.

Some of this trail already exists implicitly — §6's soft delete keeps
`deleted_at`, and rows carry `created_at`. What is genuinely missing is **category
change history** and **who acted**, and "who" only becomes a real question when
§16 puts several people in one household.

### The call

**Names are pinned here** so they are an interface rather than something each
task invents: the module is **`kanakko/eventlog.py`** (not `logging.py` — sitting
beside `import logging` is a trap for a reader even though Python 3's absolute
imports make it safe), the call is **`log_event`**, the seam is
**`bind_sink`/`unbind_sink`**, the audit table is **`transaction_events`**, the
origin field is **`source`**, and
the environment reads **`LOG_DIR`**, **`TRACE_MODE`** (default on) and
**`TRACE_KEEP`** — unprefixed, matching `SIGNUP_MODE` and `DATABASE_URL` rather
than inventing a `KANAKKO_` convention that exists nowhere else here.

`ha-backend` names its call `logStep` around a `documentId` and a `stepName`.
Those are its domain, not this one: here the unit is a Telegram update, so the
correlation id is `update_id` and the noun is an event.

One function, and its signature is the seam:

```python
log_event("transaction.confirmed", status="ok", update_id=…, user_id=…,
          duration_ms=…, txn_id=…, amount=…)
```

- **The event name and status are the greppable identifier** — the flat-file
  equivalent of `ls | grep __error` is `jq 'select(.status=="error")'`.
- **It never raises.** Every sink write is wrapped; a logging failure must never
  fail a webhook. Telegram would redeliver a message that actually succeeded.
- **Writes go through a module-level sink, bound once at startup.** Swapping the
  backing store later — a remote aggregator, a database table, a hosted service —
  is a new sink and **zero call-site changes**. This is why `ha-backend` made its
  signature `async` despite being synchronous; a bound sink achieves the same in
  Python without forcing `await` through handlers that are otherwise sync.
- **Unbound is a silent no-op**, so unit tests need no mock. `LOG_DIR` unset is
  what leaves it unbound, so `uv run pytest` writes no files without a fixture
  saying so — the sink binds inside the existing `configure_logging()`, which is
  already idempotent and already called from all four entry points (the web app
  and each of the three jobs). A second startup hook would be a second list of
  entry points to keep in step.

**Every event carries a `source`** — `webhook` | `miniapp` | `cron`. Decided
2026-08-07: two of the six money paths have **no `update_id` at all**, because
`/app/delete` and `/app/category` are HTTP routes rather than Telegram updates.
The event name alone could imply the origin, but "everything done from the
dashboard" deserves to be a filter, not a name-matching exercise. So `update_id`
is absent on a Mini App event and **nullable in `transaction_events`**, and
`source` is the field that is always present.

**Three statuses, and no more:** `ok`, `error`, `noop`. `noop` earns its place
because the redelivery paths are neither of the other two — a Confirm whose
pending row is already gone, a `/undo` with nothing to undo, a dashboard delete of
an already-deleted row. Folding those into `ok` inflates the count of confirms
that actually happened; folding them into `error` makes an ordinary double-tap
look like a defect.

**The error status is logged once, in the webhook, not in seven handlers.** The
handlers deliberately let exceptions escape so the webhook 500s and Telegram
redelivers (§14, and the Phase 1 confirm contract). A `try/except` that logs and
re-raises around the dispatch keeps that behaviour and still gives every failed
update a line; the per-handler calls then only ever report `ok` or `noop`.

**Amounts go in the operational log too, not only the audit row.** "My total is
wrong" should be answerable from the log before anyone opens Postgres. It costs
nothing extra: the audit row's before/after needs the whole row anyway, so each
money function returns the row it touched, and the log line and the audit row read
from the same value. This is a deliberate widening of what sits on the volume, and
it is covered by the trace-mode revisit below rather than being a separate
question.

**Never logged, and a scrubber as the backstop:** the bot token, the OpenRouter
key, the full `initData` string, and full LLM prompts. The scrubber redacts
anything key-shaped and any base64 run over 500 characters, but call sites must
not pass them in the first place — a scrubber is a net, not a policy.

### Trace mode: on by default, and reviewed before real customers

**Decided:** the `ha-backend`-style artefact folder, per update, **enabled by
default**, gated by an env var so it can be turned off without a deploy.

**What that means, stated plainly:** every message a user sends — the raw text,
the prompt built from it, the model's response — is written to disk and kept until
rotation. That is exactly what makes a hard parse bug diagnosable, and it is the
right trade during a beta among friends.

**It is the wrong trade once strangers pay for this**, and the decision is
recorded so it is not discovered later: before the first paying customer, this
must be revisited against whatever §16's `UNVERIFIED` data-protection question
resolves to. Rotation bounds the disk, not the exposure.

**Rotation:** the last N update folders, N from an env var. The volume is small
and shared with the operational log.

### Storage

A single volume mounted on **both** `kanakko-web` and `kanakko-cron` — they are
separate Dokploy applications and the jobs log too. Only `pgdata` exists today, so
this is new infrastructure and a `[human]` step.

Rotation uses stdlib `logging.handlers`; **no new dependency.** `structlog` was
considered and rejected: its value is context binding, and a correlation id passed
explicitly is clearer than one bound ambiently — and one fewer dependency in a
project whose §7 and §8 are largely about what was left out.

---

## 18. Accounts: money has a location

Decided 2026-08-12, after working through how savings, credit cards and family
money actually behave.

**The problem.** A transaction has a type (`expense` | `income`) and no location.
That is enough to answer "what did I spend last month" and nothing else. Three
things break on it at once:

- **Money that isn't spent.** An SIP debit, an FD, a chit instalment leaves the
  bank without leaving your net worth. Logged as an expense it inflates spending
  and hides savings; not logged at all, income minus expenses no longer explains
  what is in the bank.
- **Money that comes back.** FD maturity, a chit payout, redeeming a fund, a
  friend repaying a loan. Logged as income, the month reads as a windfall and
  every average is wrong. A chit payout is overwhelmingly *your own money
  returning*; only the forgone discount was ever a cost.
- **Credit cards.** A swipe is spending but no cash moves; paying the bill moves
  cash but is not spending. With one undifferentiated pool, both count, and every
  card user's spending is roughly doubled.

**Decided: an account is a named pool of money with a kind and an opening
balance.** Not double-entry — no debits, no credits, no chart of accounts. Four
kinds cover everything above:

| Kind | Examples | Balance behaviour |
|---|---|---|
| `spending` | Bank, Cash | Falls when you spend. **UPI is not a kind** — it is a rail that pulls from the bank |
| `credit` | HDFC card | You *owe*. A swipe increases the debt; paying the bill is a transfer from a `spending` account |
| `pot` | SIP, FD, Chit, RD, "Lent — cousin" | Money parked. Contributions in, maturity out. **Contributions only, never market value** |
| `virtual` | Opening balance, Adjustment | The counterparty for money with no real origin (below) |

**A `pot` never carries a market value.** It knows what you put in and what came
back, both of which are facts. What an SIP is *worth today* needs NAV feeds, unit
counts and cost bases — a different product, and one the user's broker already
provides. Stating this limit is the decision, not an omission.

**A fourth transaction type: `transfer`,** carrying `from_account` and
`to_account`. It is **excluded from every spending and income total** — the rule
every mature tracker converged on independently ([Monarch](https://help.monarch.com/hc/en-us/articles/360048393292-Transfers-and-Credit-Card-Payments):
transfers are "excluded from your budget, cash flow, and spending totals because
they aren't new spending"; [Copilot](https://help.copilot.money/en/articles/3971267-transaction-types):
Internal Transfers are "excluded from your spending budgets";
[Firefly III](https://docs.firefly-iii.org/references/firefly-iii/transaction-types/):
a transfer structurally cannot carry a budget).

One mechanism then answers six separate features, which is why it is worth the
schema change:

| The thing | Is just |
|---|---|
| SIP / FD / chit contribution | transfer, Bank → pot |
| FD maturity, chit payout, loan repaid to you | transfer, pot → Bank |
| ATM withdrawal | transfer, Bank → Cash |
| Credit card bill payment | transfer, Bank → card |
| Sending money to a family member | transfer between two accounts in the household |
| Opening balance | transfer, `virtual` → the account |

**The ATM case is the one that proves the rule.** Without transfers, people log
the withdrawal *and* the cash spending, double-counting every rupee they take
out.

### Rules that fall out, and must not be re-litigated in code

**Opening balance is not income.** It is a transfer from the `virtual` account.
Income means money that arrived from outside; a starting figure is a position,
not an event. Leaking it into income puts a fake windfall in month one.

**For a `credit` account the opening balance is what you owe,** not what you
have. Same column, different question at onboarding — a card asking "how much is
in it?" is nonsense.

**Every user gets a default account,** created at onboarding, and a transaction
that names no account lands there. This is what keeps the daily path unchanged:
"spent 500 on tea" must never gain a tap. Accounts become visible only when a
second one exists.

**Show the account, never ask for it.** The confirm card displays the account the
parse chose, with one tap to change — the pattern §3 and §5 already use for
category. "Swiped 2000 on dinner", "paid cash", "put 5000 in SIP" carry the
answer; asking every time taxes the 80% case to serve the 20%.

**The account enum in the parse schema is per-user, built per request.** This is
a deliberate departure from §11, where the closed category set is declared once
and derives everything. Accounts are the user's own nouns, so the schema handed
to the model is assembled from *that household's* accounts at request time. §11's
reasoning still holds for what it governs — the model must not invent an account
any more than it may invent a category — but "defined once" becomes "derived once
per request", and the general prohibition on a second source of truth is
unchanged: the account list comes from the accounts table, never a literal.

**Accounts belong to the household, and each names an owning member.** §16's
shared ledger already means everyone sees everything, so this needs no new
visibility rule. Member-to-member money is a transfer between two of the
household's accounts — no separate feature, no separate concept.

**Balance is derived, never stored:** `opening_balance + inflows − outflows`,
computed from the ledger. A stored running total is a second source of truth that
drifts silently, which is the one failure mode a money app cannot survive.

### Refunds are negative spending, linked to what they refund

A refund is not income. Returning a ₹2,000 shirt is ₹2,000 of `Shopping`
un-spent, not ₹2,000 earned; booked as income it inflates both sides of the
ledger and corrupts the savings rate.

**A refund references the transaction it refunds and may be partial** — an amount
up to, not necessarily equal to, the original. The constraint that makes it
trustworthy: **the sum of refunds against a transaction can never exceed it.**
That guard must be caused to fail before it is believed (CLAUDE.md).

Linking is by choosing, not by parsing. `refund 500` lists recent candidates —
amount-matched first — and one tap picks the row, reusing the chooser keyboard
`/remove` already has. A Refund action on the dashboard row covers older entries.

**`Refund` is removed from the income category list** (§11), where it currently
sits and quietly inflates income.

### Recurring debits ask, they do not fire silently

A recurring rule (amount, category, account, day of month, active) is the answer
to auto-debits, which are the worst case for a manual tracker: the money moves
and *nothing prompts the user*, because they never saw it happen.

**On the day, the cron sends the ordinary confirm card** — "SIP ₹5,000 today?"
— with Confirm / Change amount / Skip. Not a silent insert. Three reasons: it
reuses the entire §4 confirm machinery rather than building a second write path;
**a chit instalment changes every month** as the dividend reduces it, so a fixed
auto-entry is wrong nearly every time; and a silently wrong row is worse than a
missing one, because nobody knows to look for it.

### Reconciliation is a nudge and a visible adjustment

Nothing is connected to a bank, so the ledger drifts, and a ledger you stop
trusting is one you stop feeding. Weekly, per account: *"I think your Bank has
₹42,300 — what does your bank say?"* A different figure writes an **adjustment**
row against the `virtual` account.

**The adjustment is a visible ledger row, never a silent correction.** A number
quietly rewritten to match is how a ledger starts lying.

**Rejected:**

- *A third `type` (`saved`) instead of accounts.* It answers exactly one of the
  three problems above. Credit cards, cash, per-account reconciliation, opening
  balances and family transfers would each arrive as another flag, another
  migration, another special case — accounts, built badly, one at a time.
- *Double-entry.* Correct and far too much ceremony for a chat bot. A pool with
  an opening balance gets every number this product shows.
- *Portfolio value.* See `pot` above.
- *Per-member privacy inside a household.* It makes every total ambiguous — "is
  this everything, or everything I may see?" — for a case that already has an
  escape hatch: `/invite_signup` gives anyone their own household.
- *Statement upload, now.* Formats vary by bank with no standard and PDFs are
  commonly password-protected, and matching parsed rows against logged ones is a
  second problem on top of parsing. The reconcile nudge buys most of the same
  trust for a fraction of the work; revisit only if drift persists after it
  ships. **India's regulated account-aggregator framework may be the better route
  than parsing files at all — UNVERIFIED, nobody has checked what it takes for an
  individual developer to use it.**

---

## Deliberately deferred

Each gets a `ponytail:` comment in the code naming its ceiling and upgrade path.

| Deferred | Add when | Replace with |
|---|---|---|
| ~~Multi-tenancy~~ | ~~A second user exists~~ | **Trigger fired 2026-08-07 — see §16.** The schema was ready for per-user isolation; shared ledgers needed a second axis it did not anticipate |
| Cheaper model | ~1,000 users (~₹25k/mo on Opus 5) | OpenRouter config change |
| Telegram send throughput | ~50,000 users | Rate-limited send loop or paid broadcasts — **not** a task queue |
| User-defined categories | Repeatedly forcing entries into `Other` | Category table per user |
| Bank statement import | Drift persists *after* §18's reconcile nudge ships | CSV for the two or three banks testers actually use — not PDF, not "all banks". Check the account-aggregator route first |
| Portfolio / market value | Never, by decision (§18) | The user's broker already does it |
| Loan EMI principal/interest split | A user asks why their loan balance never moves | An amortisation schedule per loan; until then the whole EMI is an expense |
| Alembic | Schema churns weekly | SQLAlchemy Core + Alembic |
| Charting library | A view genuinely needs one | Decide then; CSS bars until |
| APScheduler / queue | Sidecar cron proves awkward | In-process scheduler (mind the replica hazard) |

**Verified constraint behind the throughput row** (Telegram Bot API FAQ):
*"In a single chat, avoid sending more than one message per second"* and *"For
bulk notifications, bots are not able to broadcast more than about 30 messages
per second, unless they enable paid broadcasts"* (up to 1000/sec paid). At
30/sec, a 10,000-user noon reminder drains in ~5.5 minutes — fine. At 50,000 it
is ~28 minutes; at 100,000 it is ~56 minutes and the noon batch is still sending
when the evening batch is due. **Telegram's rate limit is what breaks first —
not the database.**
