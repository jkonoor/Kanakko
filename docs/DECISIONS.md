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
model is a config value. Default model `claude-opus-5`. Request structured
output via `response_format: {type: "json_schema", ...}` with
`require_parameters: true` in provider preferences.

**Why:** Cost is not a constraint at this scale. Verified first-party rates
(cached 2026-06-24): Haiku 4.5 $1/$5 per MTok, Sonnet 5 $3/$15, Opus 5 $5/$25.
A parse is ~300 input + ~60 output tokens, so roughly ₹0.05–₹0.25 per
transaction — under ₹40/month at personal volume. Opus 5 is chosen for
extraction accuracy; cost only starts mattering around ~1,000 users, and that
is exactly the switch OpenRouter exists to make cheap.

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

## Deliberately deferred

Each gets a `ponytail:` comment in the code naming its ceiling and upgrade path.

| Deferred | Add when | Replace with |
|---|---|---|
| Multi-tenancy | A second user exists | Users table; stop hardcoding the ID. Schema and Mini App auth are already ready |
| Cheaper model | ~1,000 users (~₹25k/mo on Opus 5) | OpenRouter config change |
| Telegram send throughput | ~50,000 users | Rate-limited send loop or paid broadcasts — **not** a task queue |
| User-defined categories | Repeatedly forcing entries into `Other` | Category table per user |
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
