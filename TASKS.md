# Task queue

Derived from [`docs/PLAN.md`](docs/PLAN.md). Worked top to bottom — the order is
the plan's order and it matters.

**Conventions**

- `- [ ]` open · `- [x]` done. The dev loop ticks the box in the same commit as
  the change.
- `[human]` tasks are **skipped by the loop** — they touch credentials,
  deployment, or an external account and are done attended.
- QA findings do **not** live here. They go in `REVIEWS.md`, and the implementer
  fixes open ones before touching this queue again.
- One task is one iteration. If a task turns out to be two things, split it and
  do the first.

---

## Phase 0 — Walking skeleton

- [x] Add `pyproject.toml` (fastapi, uvicorn, psycopg[binary], python-telegram-bot, pydantic, httpx, pytest) and `uv.lock`
- [x] Add `kanakko/app.py` with a `/healthz` endpoint returning 200 and the app version
- [x] Write `migrations/001_init.sql` — `users`, `transactions`, `pending_transactions`, `reminder_log`, and the `active_transactions` view, per the data model in `docs/PLAN.md`
- [x] Add a migration runner that applies `migrations/*.sql` in order and records which have run — while doing it, actually attempt to execute `001_init.sql` against Postgres (`psql -f`, or `docker compose up db`) and record the command and its real output; that DDL has never been parsed by a server (`REVIEWS.md` finding 5 on `4b92212`)
- [x] Add `docker-compose.yml` with `web`, `db` (postgres), and `cron` services; `cron` uses the same image with a cron command and `TZ=Asia/Kolkata`
- [x] Add `.env.example` with every required key and empty values — carry the `POSTGRES_PASSWORD` constraint from `REVIEWS.md` finding 2 on `4341744` onto the key itself: it is interpolated into `DATABASE_URL` unencoded, so `[A-Za-z0-9._~-]` only
- [x] `[human]` Create a GitHub PAT with `read:packages` for Dokploy to pull the private GHCR image
- [x] `[human]` Create the `Kanakko` project on doc-panel with three resources: `kanakko-db` (Postgres), `kanakko-web` and `kanakko-cron` (Applications, Docker provider) — see `docs/DEPLOYMENT.md` → Topology
- [x] `[human]` Set `DOKPLOY_DEPLOY_WEBHOOK` as a repo secret from the `kanakko-web` service UI, and run the deploy workflow
- [x] `[human]` Attach the domain to `kanakko-web` and verify `/healthz` returns 200 over HTTPS

## Phase 1 — The core loop

- [x] Add `kanakko/categories.py` — the expense and income lists from `docs/DECISIONS.md` §11, plus helpers that emit the JSON-schema enum and the Telegram keyboard from the same constant
- [x] Add `kanakko/money.py` — Decimal parsing/formatting for ₹ amounts, rejecting float anywhere
- [x] Add a check covering parse → store → sum-by-category using `Decimal`
- [x] Add `kanakko/parse.py` — OpenRouter call with `response_format` JSON schema and `require_parameters: true`, per `docs/DECISIONS.md` §2
- [x] Make `parse.py` inject the current `Asia/Kolkata` date into every prompt so relative dates resolve
- [x] Add Pydantic validation of the parse result plus exactly one retry on schema failure
- [x] Make `category` nullable in the schema and `amount` non-nullable, per `docs/DECISIONS.md` §3
- [x] Add the Telegram webhook endpoint and update dispatch
- [x] Reject `/webhook` with 403 unless `X-Telegram-Bot-Api-Secret-Token` matches `TELEGRAM_WEBHOOK_SECRET`, compared with `hmac.compare_digest`; **fail closed when the secret is unset** — per `docs/DECISIONS.md` §15. Do this **before** the Confirm handler: that is the commit where a forged update starts writing rows
- [x] Add `TELEGRAM_WEBHOOK_SECRET` to `.env.example` and to compose's shared app env with `:?`, alongside the other required keys
- [x] Render the confirm card: amount, type, category, date, note, with Confirm and Cancel buttons
- [x] Add `kanakko/db.py` — connection + the confirm-flow persistence: `save_pending`
      (parsed row keyed by the card's message id) and `confirm_pending` (read →
      insert into `transactions` → delete the pending row, atomically). Carved off
      task 56 below: this is its DB logic, tested against Postgres. The amount
      travels as a JSON string and returns through the §9 door (`parse_amount`),
      so a float can't reach the ledger; `confirm_pending` returns `None` on a
      redelivered tap so a confirm never double-writes.
- [x] Add `kanakko/tg.py` — the Telegram-send client split out of Handle Confirm:
      `answer_callback_query`, `send_message`, `edit_message_text`, raw httpx
      against the Bot API (mirroring `parse.call()`), token from
      `TELEGRAM_BOT_TOKEN`, fails closed when unset. `reply_markup` accepts the
      `InlineKeyboardMarkup` `confirm_card` returns and serialises it via
      `.to_dict()`. Tested without network (`tests/test_tg.py`).
- [x] Split off the text-message handler: `app.handle_text(conn, msg)` — resolve
      the user (`db.get_or_create_user`, new), parse the text (`parse_message`),
      send the confirm card (`tg.send_message`), and `db.save_pending` keyed by
      the *sent card's* message id. Standalone + tested without network
      (`tests/test_webhook.py`, `tests/test_db.py`); not yet wired into `/webhook`.
      Null-category cards still render `Category: None` — that branch is task 66.
- [x] Handle Confirm — wire it up in `app.py`: route `TextMessage` in `/webhook`
      to `handle_text`, and add the `ButtonPress(data=CONFIRM)` handler — call
      `db.confirm_pending`, then acknowledge over Telegram with `kanakko/tg.py`.
      The message handler and send client both exist now (tasks above); this task
      opens the DB connection, commits, and connects the pieces in the endpoint.
      A handler exception is left to 500 so Telegram redelivers a transiently
      failed transaction; only CONFIRM routes here — Cancel is the next task.
- [x] Handle Cancel — discard the pending row, acknowledge
- [x] Reject messages with no parseable amount with a rephrase prompt, storing nothing
- [x] Show category buttons instead of the confirm card when `category` came back null

## Phase 2 — Corrections

- [x] Category buttons on the confirm card, generated from `categories.py`
- [x] Handle a category button press — update the pending row, re-render the card
- [x] Add `/undo` — soft-delete the most recent confirmed transaction, confirm what was removed
- [x] Verify every read path goes through `active_transactions`, not `transactions`

## Phase 3 — Scheduled jobs

- [x] Configure logging at startup so `log.info` actually emits — `logging.getLogger(__name__)` with nothing configured means Python's last-resort handler drops everything below WARNING, so an `log.info` would be silently discarded under uvicorn. Verified live: a real Telegram delivery produced uvicorn access lines and nothing from the app. **Correction (2026-08-06):** the app has no logging calls at all — `grep -rn "logging" kanakko/` returns nothing, and it never has. So this task is: configure logging at startup *and* give the app something to log, so the config is exercised rather than asserted. Add a check that an INFO record from a `kanakko` logger reaches a handler. Do this **before** the jobs below — their only evidence of having run is a log line

- [x] Add `kanakko/jobs/evening.py` — 21:00 daily, unconditional, day's total and entry count
- [x] Add `kanakko/jobs/noon.py` — 12:00 daily, suppressed if anything was logged since the last evening summary
- [x] Add `kanakko/jobs/monthly.py` — 09:00 on the 1st, previous month's income, expenses, balance, top categories
- [x] Write `reminder_log` rows from every job; make the noon suppression read it
- [x] Add the crontab for the `cron` service with all three entries
- [x] Add a check that a transaction at 23:50 IST on a month's last day lands in that month's report

## Phase 4 — Mini App dashboard

- [x] Add `initData` HMAC validation — `HMAC-SHA256(bot_token, "WebAppData")` as the secret key, per `docs/DECISIONS.md` §13
- [x] Add a check that a forged or tampered `initData` payload is rejected
- [x] Add the dashboard route rendering totals, balance, and current-month figures
- [x] Add the category breakdown as a sorted list with CSS percentage bars — no charting library
- [x] Add the weekly and monthly summary sections
- [x] Add the recent-transactions list with per-row soft delete — **two constraints
      that arrive with this task, both noted attended on `62f5708`:**
      **(a) escape the `note`.** This is the first task to render a user-typed
      string into server-rendered markup. `webapp.py` currently says "no
      user-controlled string reaches the markup here, so there is nothing to
      escape yet" — that stops being true here. §11 keeps the note's original
      wording deliberately, so `note` is arbitrary text that arrived through the
      bot. Unescaped, `<img src=x onerror=...>` in a logged expense is stored XSS
      in the dashboard. Use `html.escape`, and add a check that a note containing
      `<script>` renders inert — assert the *escaped* bytes, not that the page
      "looks fine".
      **(b) add the `auth_date` freshness check.** ✅ **Landed as the prerequisite
      split — this box stays open for the recent-list + delete + note-escaping
      remainder.** `validate_init_data(init_data, max_age=..., now=...)` now opts
      into the check: with `max_age` set, a missing/malformed `auth_date` or one
      older than `max_age` raises `InitDataError`; the read-only `/app/data` route
      still passes no `max_age`. Tested (`test_webapp.py`): stale rejected, fresh
      passes, missing fails closed. **The mutation route this task adds MUST call
      `validate_init_data(..., max_age=timedelta(hours=24))`** — the guard exists
      but is only wired once a state-changing route uses it. (Was: Telegram's
      docs — "check the `auth_date` field"; the official SDK defaults to
      `expiresIn = 86400`.)
- [x] Add per-row category change from the dashboard — same two constraints as the
      task above if they haven't landed yet; category itself is a closed set from
      `categories.py`, so the escaping risk here is the note, not the category
- [x] Make the dashboard render correctly in both light and dark themes
- [x] `[human]` Register the Mini App menu button with BotFather — done 2026-08-07; the Mini App opens and its `initData` HMAC verifies against real Telegram payloads, which is what settled the URL-decoding assumption no unit test could reach

## Phase 5 — Backups and hardening

- [ ] `[human]` Create the Dokploy backup for `kanakko-db` (`backupType: "database"` — the managed resource, so no `metadata` field needed) — see `docs/DEPLOYMENT.md`
- [ ] `[human]` Trigger a manual backup, restore it into a scratch database, verify the row count
- [ ] `[human]` Confirm every secret lives in Dokploy env and none is in the repo


## Phase 6 — Upstream failure handling

**Status: open — this is the next loop-able work.** Every Phase 5 task is
`[human]`, so the loop takes the first task below.

Raised 2026-08-07 from a real outage: the bot was silently dead in production
for the whole of Phases 0-4. `parse.call()` raised, `/webhook` 500'd, Telegram
redelivered forever, and the user saw *nothing at all* — no card, no error, no
hint. Two separate upstream failures hid behind that silence (a 402 for exhausted
OpenRouter credits, then a 400 for the type-array schema, fixed in `f1320a1`).

- [x] Tell the user when a parse fails for a non-transient reason, instead of
      failing mute. `handle_text` already converts a `ValidationError` into the
      rephrase prompt (§3); this extends that to upstream failures. An
      `httpx.HTTPStatusError` whose status is **4xx** is permanent — a bad key
      (401), exhausted credits (402), a rejected schema (400) — and no amount of
      redelivery fixes it, so answer the user with something like "I can't reach
      my parser right now — your message wasn't saved, try again shortly" and
      return 200 so Telegram stops retrying. **5xx and network/timeout errors
      must keep 500ing**, because those *are* transient and redelivery is the
      recovery — that behaviour is deliberate (see the Handle Confirm task in
      Phase 1) and must not regress. Store nothing in either case.
      The check that earns its place: a stubbed `parse_message` raising a 402
      results in a sent message and a 200, and one raising a 503 results in no
      sent message and the exception propagating. Assert both directions — a test
      that only covers the 4xx branch would pass if every error were swallowed,
      which is the more dangerous bug.
- [x] Log the upstream failure at `WARNING` with the status code and provider
      body, so the next occurrence is diagnosable from the container log rather
      than by reconstructing the request by hand. `configure_logging` (task 84)
      already makes this visible; nothing currently logs it. Keep the API key out
      of the log line — it is in the request headers, not the body, but assert
      that in the check.

## Phase 7 — Dashboard context: period-over-period comparison

**Status: open, deferred by the user 2026-08-07** — Tiers 1 and 2 of the dashboard
review (tap targets, accessible labels, period switcher, hero figure, bar
percentages) were done attended; this is Tier 3, held back deliberately because it
is the only part needing new SQL.

The dashboard answers "what did I spend?" but never "is that a lot?", which is
what makes a finance view worth opening more than once. The design research behind
this: pairing a figure with a directional delta *"shows both the direction and
scale of change"* (Smashing Magazine, *UX Strategies for Real-Time Dashboards*,
2025-09) — a number with no baseline is a record, not an insight.

- [x] Add previous-period figures to the dashboard so each headline carries a
      delta — `₹300 · ▼ 40% vs last month`. `db.month_summary` already takes an
      arbitrary half-open range, so the previous month/week is the same query with
      shifted bounds and needs no new SQL shape — but it *is* a second query per
      period, so decide whether to widen the existing function or call it twice.
      Boundaries stay `Asia/Kolkata` (§10); the previous month of January is
      December of the prior year, which is the case the check must cover.
      A delta against a zero baseline is undefined, not 100% — a first-ever month
      must render "no comparison yet" rather than a fabricated percentage. That is
      the guard worth writing: seed one month of data only, assert no percentage
      is shown; then seed two and assert the sign and magnitude are right.
      Colour follows the same rule as the rest of the dashboard: direction is
      carried by the arrow glyph and the label, never by colour alone (WCAG 1.4.1),
      and expenses stay in normal ink — the one accent is reserved for income.
- [x] Consider a per-day bar for the current week, using the existing
      `active_transactions` reads. **Decided: declined — does not earn its place.**
      The hero already answers "what did I spend this week" and the new
      period-over-period delta answers "is that a lot?"; the category breakdown
      answers "on what". Seven daily bars of a *current, incomplete* week add
      noise, not a decision: a sparse personal ledger leaves most days at 0–2
      transactions (mostly empty bars), and a week-to-date view pits a full
      Monday against a partial Sunday, so the day-to-day comparison isn't even
      honest. The "you spend on weekends" pattern needs many weeks, not this
      partial one. No code changed; the gate ("only if it changes a decision") is
      the deliverable and the answer is no. If a future multi-week view ever
      revisits this, length-based bars remain the right form (NN/g: length and 2D
      position are what people judge accurately; pie charts *"should be avoided
      most of the time"*), so it stays CSS, no charting library (§13).

## Phase 8 — Logging, audit trail, and trace mode

Implements [`docs/DECISIONS.md`](docs/DECISIONS.md) **§17**. Read it first.

**Ordered so the seam exists before the call sites.** Task 1 is the only one that
is hard to change later — every task after it is call sites and sinks.

**Decided 2026-08-07: this phase runs before households.** §16 puts other
people's money in the ledger and the audit's finding was that no money path leaves
any trace at all, so the trace should exist *before* the household logic is
written rather than be retrofitted around it. Task 1 is the seam; everything in
this phase and the next logs through it. The phases were renumbered so the file
order the loop obeys matches the numbering.

- [ ] Add `kanakko/eventlog.py`: `log_event(event, *, status, **fields)` writing
      JSON Lines through a **module-level sink bound once at startup**, plus
      `bind_sink` / `unbind_sink`. Unbound is a silent no-op so tests need no
      mock (§17). **It must never raise** — wrap every write, because a logging
      failure that 500s a webhook makes Telegram redeliver a message that already
      succeeded. The check that earns its place: a sink that raises on every call
      leaves `log_event` returning normally *and* the caller's work intact.
- [ ] Add the scrubber and the never-log list (§17): redact key-shaped strings and
      any base64 run over 500 characters, recursively through nested values. The
      check must pass a realistic payload — a bot token, an `sk-or-` key, a long
      base64 blob — and assert each is absent from the output *and* that the
      surrounding fields survived. A scrubber that redacts everything passes a
      naive test.
- [ ] Log every money mutation (§17) — confirm, cancel, `/undo`, category change,
      dashboard delete and recategorise. Each carries `update_id` as the
      correlation id, `user_id`, `duration_ms`, and the transaction id. This is
      the audit's original finding; six handlers currently log nothing.
- [ ] Log the parse path: the OpenRouter call's duration, model, and outcome. The
      Phase 6 `WARNING` already covers the failure; this adds the success side, so
      a slow model is visible before it becomes a complaint about the bot feeling
      sluggish.
- [ ] Add `migrations/003_transaction_events.sql` and write an audit row **in the
      same transaction as the money change** (§17). Columns: the transaction, the
      acting user, the action, before/after as `jsonb`, the `update_id`, and when.
      The point is atomicity, so the check is the one that proves it: a handler
      that raises after the ledger write must leave **neither** the transaction nor
      its audit row — assert both are absent, not just one.
- [ ] Add trace mode (§17): a per-update artefact folder, **on by default**, gated
      by an env var, with the outcome in each filename so a directory listing is
      the summary. Rotate to the last N folders, N from env. The check: a failed
      parse leaves a file whose *name* identifies the failure, and the rotation
      actually deletes — a rotation that never fires is the bug that fills a disk.
- [ ] Add `LOG_DIR`, `TRACE_MODE` and `TRACE_KEEP` to `.env.example` and to
      compose's shared app env, alongside the other keys. **`.env.example` carries
      keys with empty values** (CLAUDE.md) and `tests/test_compose.py` enforces
      both that and the rule that every documented key is consumed by a service —
      so the defaults live in compose (`${TRACE_MODE:-on}`) or in code, the way
      `OPENROUTER_MODEL` already does. Note the §2 trap: compose's `:-` makes the
      variable *present but empty*, so read it with `or`, not `get(key, default)`.
- [ ] `[human]` Mount a volume on **both** `kanakko-web` and `kanakko-cron` in
      Dokploy — they are separate applications and the jobs log too. Only `pgdata`
      exists today. Verify a container restart preserves the log, which is the
      whole reason for the volume: without it every deploy wipes the evidence.

## Phase 9 — Households, onboarding, and access control

Implements [`docs/DECISIONS.md`](docs/DECISIONS.md) **§16**. Runs *after* Phase 8 — the money paths log through the seam that phase builds. Read it first — every
rule below comes from there, and the ambiguous cases were decided by the user on
2026-08-07, not left to judgement.

**Order matters more than usual here.** The access gate (tasks 1–3) closes a live
hole: the bot is already multi-user, so anyone who finds it gets a ledger parsed on
the operator's OpenRouter credits. That ships before the schema work. The identity
fix (task 2) must land *before* the household schema, not after.

**Nothing in this phase may be deployed half-done.** A partial household migration
means transactions with no home. Each task leaves the tree green and deployable.

### Access control — closes a live hole, ship first

- [ ] Separate identity from delivery address. `handle_text` uses `msg.chat_id` as
      the user's identity and never reads `message.from.id`; in a private chat the
      two coincide, so it works today and is wrong the moment a household or a
      group exists. Capture `from.id` in `dispatch` onto `TextMessage`/`ButtonPress`,
      resolve the *user* from it, and keep `chat_id` purely as the send target. The
      check: an update whose `from.id` differs from `chat.id` resolves the user by
      `from.id` — that guard is impossible to write today and is the whole point.
- [ ] Add the `invites` table and `SIGNUP_MODE` (env, `invite` | `open`, default
      `invite` — fails closed like §15's secret). Columns: `code` unique, `kind`
      (`signup` | `household`), `household_id` nullable, `label`, `created_by`,
      `used_by` nullable, `used_at`, `expires_at`. Single-use: a code with
      `used_by` set is spent. §16 keeps the two kinds distinct on purpose — a
      household invite implies signup, a signup invite joins nobody.
- [ ] Gate every inbound update on authorization, before any LLM call. An
      unrecognised user in `invite` mode gets a polite refusal and **nothing is
      stored — not even a user row** (§16). The check that earns its place: an
      unknown user's message creates no rows *and* makes no OpenRouter call, since
      the cost is the reason this exists. Assert both.
- [ ] Add the per-user daily cap — env var, default 50, never hardcoded (§16).
      `processed_updates` already stores one row per handled update but only
      `update_id`; add `user_id` and count per IST day. Enforce *before* the LLM
      call. The check: the 51st message in a day is refused and costs nothing,
      and the count rolls over at IST midnight, not UTC (§10).

### Households — the schema change

- [ ] Add `households` (owner, `plan` default `'beta'`, created_at) and
      `household_members`, and migrate every existing user to a household of one.
      The migration is the risky part: it must be idempotent, and a user must end
      up in exactly one household (§16). Check it against a seeded multi-user
      database, not an empty one.
- [ ] Move the ledger's tenancy axis: `transactions` gains `household_id` (whose
      money) while `user_id` becomes "who entered it" (§16). Backfill from the
      household-of-one mapping. **This is the task that can corrupt the ledger** —
      the check must prove every pre-existing transaction still appears in exactly
      one household's totals, with the same sum as before the migration.
- [ ] Re-scope every read to the household: `day_summary`, `month_summary`,
      `logged_since`, `recent_transactions`, `undo_last`. The §6 read-path guard
      already forces `active_transactions`; extend it so a read missing a
      `household_id` predicate is caught the same way. A household read that
      leaks another household's rows is the worst bug this phase can ship.
- [ ] Enforce the per-member rules (§16): `/undo` removes **your own** last entry,
      and the dashboard's delete and recategorise act only on rows you entered.
      Two checks, both about the *other* member: A cannot undo B's entry, and A's
      delete of B's row is refused.

### Onboarding

- [ ] Add `/start` with deep-link payload handling. No handler exists today — an
      unknown user simply types and silently gets a ledger. Bare `/start` in
      `open` mode creates a household of one and explains the bot; `/start <code>`
      consumes an invite. Payload limits are **verified**: 64 chars, `A-Z a-z 0-9
      _ -` (§16). The check: a spent code, an expired code, and a garbage payload
      are each refused distinctly, and none creates a partial household.
- [ ] Add `/invite` (owner only) — issues a labelled single-use household code and
      returns the `t.me/<bot>?start=<code>` link. Label so the operator can tell
      who is active (§16).
- [ ] Add `/household` (who is in it, who owns it) and member removal: the owner
      may remove anyone, **any member may remove themselves** (§16 — owner-only
      removal traps a member in a ledger they cannot leave). One code path, the
      self case being actor == target.
- [ ] Removal asks **retain or delete** that member's entries, and deletion is
      real (§16). This is *not* §6's soft delete and must not share its name. The
      warning must say the specific consequence: hard deletion makes past reports
      stop reconciling — a month that summarised ₹18,920 will not match when
      re-opened. Checks: retain leaves totals unchanged; delete removes the rows
      and *changes* the household total, which is the point being warned about.
- [ ] Require ownership transfer before an owner can leave (§16) — a household
      always has an owner. The check: an owner's self-removal is refused while
      they still own it, and succeeds after transfer.

### Reminders under households

- [ ] Re-scope the jobs (§12, §16): evening and monthly carry **household**
      figures to every member; the noon nudge is suppressed **per person**, so a
      member who logged nothing is still nudged even if a housemate was active.
      That per-person rule is the one most easily broken by a household-wide
      `logged_since`, so it gets the check.

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
