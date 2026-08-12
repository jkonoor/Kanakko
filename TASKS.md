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

**Five things §17 assumes that the code does not currently provide.** Found by
walking every money path on 2026-08-07, before any of this was written, and
settled by the user. They are listed here once rather than rediscovered per task:

1. **`update_id` never reaches a handler.** `dispatch` (`handlers.py:60`) builds
   `TextMessage`/`ButtonPress` and drops it; the webhook reads it separately
   (`app.py:257`). §17 pins it as *the* correlation id. Both dataclasses gain
   `update_id` and `source`, set in `dispatch`. All 16 construction sites in
   `tests/test_webhook.py` are keyword-based, so only the two equality assertions
   (`:52`, `:65`) change — and they should now assert the correlation id is
   captured, which is the point.
2. **The Mini App mutations have no `update_id`.** Hence `source` and a nullable
   column (§17).
3. **`undo_last` does not return the row's id** (`db.py:168`). The audit row needs
   it as a foreign key and every money event carries it: add `txn_id` to the
   `RETURNING` list.
4. **The money functions return bare ids**, so no call site can log an amount.
   Each returns the row it touched (or `None`) instead; `undo_last` already has
   that shape. One shape across all four (§17).
5. **Nothing logs the error side**, because handlers deliberately let exceptions
   escape. One `try/except … log … raise` in the webhook, not seven (§17).

**Eight call sites, not six** — TASKS' original "category change" is two different
operations, and only one touches the ledger. `handle_category` rewrites a *pending*
row the user is still editing; `set_transaction_category` changes a *confirmed*
one, and that second is the "category change history" §17 names as the genuinely
missing audit data. Audit rows come from exactly four operations — confirm, undo,
dashboard delete, dashboard recategorise. `handle_cancel`, `handle_category` and
`handle_text` touch only pending rows: they get an operational line and no audit
row, because there is no `txn_id` to hang one on.

- [x] Add `kanakko/eventlog.py`: `log_event(event, *, status, **fields)` writing
      JSON Lines through a **module-level sink bound once at startup**, plus
      `bind_sink` / `unbind_sink`. Unbound is a silent no-op so tests need no
      mock (§17). **It must never raise** — wrap every write, because a logging
      failure that 500s a webhook makes Telegram redeliver a message that already
      succeeded. The check that earns its place: a sink that raises on every call
      leaves `log_event` returning normally *and* the caller's work intact.
      The default sink is stdlib: `json.dumps` into a `RotatingFileHandler` on a
      dedicated `kanakko.events` logger under `LOG_DIR` — that is where §17's "no
      new dependency" rotation comes from, and it is the same decision as task 6's.
      **Bind inside the existing `configure_logging()`** (`kanakko/__init__.py:6`)
      rather than adding a second startup hook: it is already idempotent and
      already called from `app.py:45` and all three jobs. **`LOG_DIR` unset leaves
      it unbound**, which is what keeps `uv run pytest` from writing JSONL into the
      repo when `tests/test_app.py` imports `kanakko.app`. Read it with `or`, not
      `get(key, default)` — the §2 compose trap. Also in this task, because it is
      the seam and not a call site: `update_id` and `source` onto both dataclasses
      (gap 1 above), and pin `ok` | `error` | `noop` as the only statuses (§17).
- [x] Add the scrubber and the never-log list (§17): redact key-shaped strings and
      any base64 run over 500 characters, recursively through nested values. The
      check must pass a realistic payload — a bot token, an `sk-or-` key, a long
      base64 blob — and assert each is absent from the output *and* that the
      surrounding fields survived. A scrubber that redacts everything passes a
      naive test.
- [x] Log the bot-side money mutations: `transaction.confirmed`
      (`handlers.py:192`) and `transaction.undone` (`handlers.py:171`), plus the
      pending-only paths that share the file — `pending.cancelled`
      (`handlers.py:210`), `pending.recategorised` (`handlers.py:235`) and
      `pending.created` (`handlers.py:102`) — and replace the bare
      `log.info` at `app.py:271` with the webhook's `update.handled`, whose
      `try/except … raise` is where `status="error"` is logged for all of them
      (gap 5). Each event carries `update_id`, `source`, `user_id`, `duration_ms`
      and — on the two ledger paths — the transaction id and amount. Gaps 3 and 4
      land here: `undo_last` gains `txn_id`, and `confirm_pending` returns the row
      rather than a bare id, which is what makes the amount loggable.
      `duration_ms` is one `time.perf_counter()` at the top of each site plus an
      `ms_since` helper in `eventlog` — no decorator, no context manager.
      The check: a redelivered Confirm logs `status="noop"`, not `ok`, and a
      handler that raises still produces exactly one `status="error"` line.
- [x] Log the dashboard money mutations: `transaction.deleted` (`app.py:158`) and
      `transaction.recategorised` (`app.py:189`), `source="miniapp"` and **no
      `update_id`** — these are HTTP routes, not Telegram updates (gap 2). The 404
      paths (already deleted, or never this user's) are `status="noop"`, and they
      are the reason `noop` exists. `soft_delete_transaction` and
      `set_transaction_category` return the row they touched, like the bot-side
      pair. **`log_event` here is a synchronous write inside an `async def`
      route** — fine at this scale, so it carries a `ponytail:` comment naming the
      ceiling and the upgrade path rather than a queue nobody needs yet.
- [x] Log the parse path: the OpenRouter call's duration, model, and outcome. The
      Phase 6 `WARNING` already covers the failure; this adds the success side, so
      a slow model is visible before it becomes a complaint about the bot feeling
      sluggish.
- [x] Add `migrations/003_transaction_events.sql` and write an audit row **inside
      the same `conn.transaction()` block as the money statement, in `db.py`, not
      in the calling handler** (§17 — read the paragraph, it explains why the
      handler version is atomic today only by accident and fails silently the
      moment §16's identity fix reorders the calls). Columns: the transaction, the
      acting user, the action, before/after as `jsonb`, `source`, a **nullable**
      `update_id`, and when. Four operations write one: confirm, undo, dashboard
      delete, dashboard recategorise. The four money functions therefore take the
      acting user and the correlation id as arguments — the accepted price of the
      arrangement that cannot come apart.
      Postgres here is 16 (`docker-compose.yml:29`), so `set_transaction_category`
      reads the old category with a `SELECT` before its `UPDATE`, in the same
      transaction, rather than reaching for a `RETURNING OLD.*` form that this
      server does not have.
      The point is atomicity, so the check is the one that proves it: a handler
      that raises after the ledger write must leave **neither** the transaction nor
      its audit row — assert both are absent, not just one. Note what that check
      does *not* prove: it exercises today's call ordering, which is exactly why
      the write's location is specified rather than left to judgement.
- [x] Add trace mode (§17): a per-update artefact folder, **on by default**, gated
      by an env var, with the outcome in each filename so a directory listing is
      the summary. Rotate to the last N folders, N from env. The check: a failed
      parse leaves a file whose *name* identifies the failure, and the rotation
      actually deletes — a rotation that never fires is the bug that fills a disk.
- [x] Add `LOG_DIR`, `TRACE_MODE` and `TRACE_KEEP` to `.env.example` and to
      compose's shared app env, alongside the other keys. **`.env.example` carries
      keys with empty values** (CLAUDE.md) and `tests/test_compose.py` enforces
      both that and the rule that every documented key is consumed by a service —
      so the defaults live in compose (`${TRACE_MODE:-on}`) or in code, the way
      `OPENROUTER_MODEL` already does. Note the §2 trap: compose's `:-` makes the
      variable *present but empty*, so read it with `or`, not `get(key, default)`.
- [x] Log the three scheduled jobs through the seam (§17). Found 2026-08-08 while
      writing the manual test rows: the jobs call `configure_logging` — which is
      what binds the sink — but **never call `log_event`**, so a `kanakko-cron`
      container writes an empty `events.jsonl`. §17's Storage section mounts the
      volume on *both* applications precisely because "the jobs log too", and
      `003_transaction_events.sql` already permits `source='cron'` with nothing
      writing it. Phase 8 had no task for this; the phase is otherwise complete.
      **One event per job run, not per user** — the fan-out is over every user, and
      a line each would put the whole ledger's shape on the volume for a summary
      that is already sent to the user. Carry `source="cron"`, the job name, how
      many users were considered, how many were delivered to, how many were
      deliberately skipped (the noon nudge suppresses active users, which is not a
      failure — see `fan_out`), and `duration_ms`. `DeliveryFailures` is the
      `status="error"` case and **must still raise**: the job exits non-zero, and
      the log line is in addition to that, not instead of it (`jobs/__init__.py`
      says so deliberately, "since logging is being designed separately" — this is
      that design arriving).
      The check that earns its place: a fan-out where one user's send raises leaves
      an `error` event **and** the earlier users' `reminder_log` rows intact. That
      is §12's failure-isolation property, which already holds and currently leaves
      no trace — the log line is what makes it observable, so assert both halves.
      In the same commit, delete the closing note in `docs/TESTING.md` §5a that
      records this gap, and add the job rows it says to add.
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

- [x] Separate identity from delivery address. `handle_text` uses `msg.chat_id` as
      the user's identity and never reads `message.from.id`; in a private chat the
      two coincide, so it works today and is wrong the moment a household or a
      group exists. Capture `from.id` in `dispatch` onto `TextMessage`/`ButtonPress`,
      resolve the *user* from it, and keep `chat_id` purely as the send target. The
      check: an update whose `from.id` differs from `chat.id` resolves the user by
      `from.id` — that guard is impossible to write today and is the whole point.
- [x] Add the `invites` table and `SIGNUP_MODE` (env, `invite` | `open`, default
      `invite` — fails closed like §15's secret). Columns: `code` unique, `kind`
      (`signup` | `household`), `household_id` nullable, `label`, `created_by`,
      `used_by` nullable, `used_at`, `expires_at`. Single-use: a code with
      `used_by` set is spent. §16 keeps the two kinds distinct on purpose — a
      household invite implies signup, a signup invite joins nobody.
- [x] Gate every inbound update on authorization, before any LLM call. An
      unrecognised user in `invite` mode gets a polite refusal and **nothing is
      stored — not even a user row** (§16). The check that earns its place: an
      unknown user's message creates no rows *and* makes no OpenRouter call, since
      the cost is the reason this exists. Assert both.
- [x] Add the per-user daily cap — env var, default 50, never hardcoded (§16).
      `processed_updates` already stores one row per handled update but only
      `update_id`; add `user_id` and count per IST day. Enforce *before* the LLM
      call. The check: the 51st message in a day is refused and costs nothing,
      and the count rolls over at IST midnight, not UTC (§10).

### Households — the schema change

- [x] Add `households` (owner, `plan` default `'beta'`, created_at) and
      `household_members`, and migrate every existing user to a household of one.
      The migration is the risky part: it must be idempotent, and a user must end
      up in exactly one household (§16). Check it against a seeded multi-user
      database, not an empty one.
- [x] Move the ledger's tenancy axis: `transactions` gains `household_id` (whose
      money) while `user_id` becomes "who entered it" (§16). Backfill from the
      household-of-one mapping. **This is the task that can corrupt the ledger** —
      the check must prove every pre-existing transaction still appears in exactly
      one household's totals, with the same sum as before the migration.
      Split: migration `007` adds the column **nullable**, backfills existing
      rows, recreates `active_transactions` (its `SELECT *` had frozen the column
      list at `001`), and adds the `(household_id, occurred_on)` report index.
      NOT NULL is deferred to the write-wiring task below — `confirm_pending` and
      ~12 test insert sites still omit `household_id`, so enforcing it now would
      break every insert.
- [x] Wire the write path to the household: `confirm_pending` sets
      `household_id` from the entering user's `household_members` row, then a
      migration makes `transactions.household_id` NOT NULL (the axis is only
      moved once new money carries it, not just backfilled rows). Check: a
      confirmed transaction lands in the confirmer's household, and a NULL
      household_id insert is refused.
- [x] Re-scope every read to the household: `day_summary`, `month_summary`,
      `logged_since`, `recent_transactions`, `undo_last`. The §6 read-path guard
      already forces `active_transactions`; extend it so a read missing a
      `household_id` predicate is caught the same way. A household read that
      leaks another household's rows is the worst bug this phase can ship.
- [x] Enforce the per-member rules (§16): `/undo` removes **your own** last entry,
      and the dashboard's delete and recategorise act only on rows you entered.
      Two checks, both about the *other* member: A cannot undo B's entry, and A's
      delete of B's row is refused.

### Onboarding

- [x] Add `/start` with deep-link payload handling. No handler exists today — an
      unknown user simply types and silently gets a ledger. Bare `/start` in
      `open` mode creates a household of one and explains the bot; `/start <code>`
      consumes an invite. Payload limits are **verified**: 64 chars, `A-Z a-z 0-9
      _ -` (§16). The check: a spent code, an expired code, and a garbage payload
      are each refused distinctly, and none creates a partial household.
      Done: `handle_start` (routed **before** the gate, since consuming an invite
      is how an unknown user becomes authorized), `db.consume_invite` +
      `db.create_household_of_one`. `tests/test_start.py`.
- [x] Add `/invite` (owner only) — issues a labelled single-use household code and
      returns the `t.me/<bot>?start=<code>` link. Label so the operator can tell
      who is active (§16). Done: `handle_invite` (routed after the gate, unmetered
      like `/undo`), `db.create_household_invite` (owner-only enforced in SQL —
      `WHERE owner = %s`), bot username from `tg.get_bot_username` (getMe, so the
      link can't name a different bot than the token). `tests/test_invite.py`.
- [x] Add `/household` — who is in it, who owns it (§16). Split from member
      removal below: `/household` is the read that removal acts *on*, and removal
      is coupled to the next two tasks (it asks retain-or-delete of entries, and
      an owner can't leave without transferring ownership first), so the display
      lands first and deployable. Done: `handle_household` (routed after the gate,
      unmetered like `/undo` and `/invite`), `db.household_roster` (household-scoped
      — owner first, each member carrying their invite label). `tests/test_household.py`.
- [x] Member removal (§16): the owner may remove anyone, **any member may remove
      themselves** (owner-only removal traps a member in a ledger they cannot
      leave). One code path, the self case being actor == target. Removal acts on
      the `/household` roster above. **An owner removing themselves is refused —
      ownership must transfer first (the task after next); until then this guard
      keeps a household from going ownerless.** Retain-vs-delete of the departing
      member's entries is the next task; this task's removal retains by default.
      Done: `/remove <label>` (owner removes that roster member) and bare `/remove`
      (leave yourself); `db.remove_member(actor, target)` holds all authorization —
      `not_owner`, `owner_must_transfer` — and re-homes the removed member into a
      fresh household of one so their next confirm doesn't hit the NOT NULL
      `household_id`; entries are retained (no `transactions` row touched). Also
      scoped `household_roster`'s label join to `i.household_id = m.household_id`
      (the `bc3b0e4` review's low finding), now reachable since a user can carry a
      used household invite from each household they've been in. `tests/test_member_removal.py`.
- [x] Removal asks **retain or delete** that member's entries, and deletion is
      real (§16). This is *not* §6's soft delete and must not share its name. The
      warning must say the specific consequence: hard deletion makes past reports
      stop reconciling — a month that summarised ₹18,920 will not match when
      re-opened. Checks: retain leaves totals unchanged; delete removes the rows
      and *changes* the household total, which is the point being warned about.
      Done: `/remove` now *asks* first — `handle_remove` shows the §16 warning with
      Keep/Delete buttons (`rm:retain:<id>` / `rm:delete:<id>`) only once
      `check_removal` says the removal is authorized; `handle_remove_choice` acts on
      the tap, re-running authorization in `remove_member` (the button is
      untrusted). `remove_member(..., delete_entries=True)` is a real
      `DELETE FROM transactions` (not §6's soft delete) that also purges the rows'
      `transaction_events` audit rows (the FK forbids orphaning them). The
      read-path guard now excludes `DELETE FROM transactions` — a write, never a
      report read.
- [x] Require ownership transfer before an owner can leave (§16) — a household
      always has an owner. The check: an owner's self-removal is refused while
      they still own it, and succeeds after transfer.
      Done: `/transfer <label>` (owner only, unmetered like `/remove`) hands
      ownership to a roster member; `db.transfer_ownership(actor, target)` holds
      the authorization (`not_owner`, `not_member`, `already_owner`). The owner's
      bare `/remove` stays refused (`owner_must_transfer`) until this moves
      ownership, then succeeds. `tests/test_transfer.py`; webhook routing +
      metering-exclusion mirror in `tests/test_webhook.py`.

### Reminders under households

- [x] Re-scope the jobs (§12, §16): evening and monthly carry **household**
      figures to every member; the noon nudge is suppressed **per person**, so a
      member who logged nothing is still nudged even if a housemate was active.
      That per-person rule is the one most easily broken by a household-wide
      `logged_since`, so it gets the check.

### Housekeeping

- [x] Split `db.py` into a `db/` package. CLAUDE.md pins this trigger to Phase 9
      ("households, memberships and invites will push it past 600") and it has
      fired: `db.py` is now 722 lines. Split by responsibility (users/households,
      pending+confirm flow, reads/reports, reminders, invites, audit), not by
      layer, keeping every import path (`from kanakko.db import …`) working via
      the package `__init__`. Pure move — no behaviour change — so the whole suite
      stays green with no test edits. Do this on its own, not folded into a
      feature task.

### Chat polish

- [x] Settle the confirm card in place on Confirm, and stop a stale Cancel from
      deleting a receipt. Raised by the user 2026-08-08 from real use: the only
      lasting evidence that a transaction was saved is a toast that disappears.
      `handle_confirm` never touches the card, so it keeps working **Confirm and
      Cancel** buttons after the row is stored.
      Two things follow, and both are fixed here:
      **(a) Edit the card into a settled state** — amount, category, date, marked
      saved, and **no `reply_markup`** — with `edit_message_text`, the mechanism
      `handle_category` already uses to re-render a card. A card in the transcript
      is permanent and far more prominent than a toast.
      **(b) `handle_cancel` must delete the card only when it actually cancelled
      something.** Today `delete_message` runs unconditionally (`handlers.py:228`),
      so a Cancel tap on an already-confirmed card removes the receipt from the
      chat while the transaction stays in the ledger — the transcript and the
      ledger then disagree. Guard the delete on `cancel_pending` having returned a
      row; the "Already gone" toast still answers the tap.
      **Do not** make Cancel's own behaviour match Confirm's: cancel means "this
      never happened" so the card goes, confirm means "this is your receipt" so it
      stays. The asymmetry is the point (§4, §5). And do not reach for
      `show_alert` to make the toast louder — it is a blocking modal, and the
      one-tap flow is what §4 and §5 exist to protect.
      The check that earns its place: after a confirm, the edited card carries no
      keyboard, **and** a Cancel arriving afterwards leaves the message in place
      and the ledger untouched. Assert both — a check that only reads the new card
      text would pass with the stale-button trap still there.
      `docs/TESTING.md` 1.2 already claims "the card stops offering Confirm" and is
      currently wrong about the code; it becomes true with this change, so no edit
      is needed there beyond adding a row for the stale-Cancel case.

- [x] Answer a non-transaction message helpfully, and without paying for it.
      Raised by the user 2026-08-08: typing "How do I use this?" today returns
      *"I couldn't find an amount in that"* — a natural question answered with a
      complaint, **after two OpenRouter calls** (`parse_message` retries once on a
      `ValidationError`, and a message with no amount fails both times). It also
      spends two slots of the user's daily cap.
      Three parts, one coherent change — they share a single string:
      **(a)** Widen `REPHRASE_PROMPT` so it orients a lost user rather than only
      correcting a failed entry. It already carries two examples; it needs to read
      as help, not rejection.
      **(b)** Add `/help`, routed beside `/undo` and `/household`, sending that
      same text. It is what people try.
      **(c)** Short-circuit the obvious greetings *before* the LLM call —
      `hi`, `hello`, `hey`, `help`, `thanks`, `what can you do`, `how do i use
      this` — on an **exact match** of the normalised message (lowercased,
      stripped, punctuation trimmed), answering with the same help text and making
      **zero** OpenRouter calls.
      **The exact match is the whole safety argument, so do not loosen it.** Any
      heuristic that decides "this isn't a transaction" — no digits, ends in a
      question mark, missing a keyword — will eventually refuse a real expense:
      "spent five hundred on lunch" has no digits, and "500 lunch" has no verb. A
      wasted LLM call costs a fraction of a rupee; a silently refused entry costs
      the trust the whole ledger runs on. Exact match cannot misfire on
      "spent 500 on hi", because that is not an exact match.
      The check that earns its place: `hi` produces the help text with the parse
      function **never called** (assert the call count, not just the reply), and
      `spent five hundred on lunch` still reaches the parser. Assert both — a check
      that only covers the greeting would pass on a filter that swallowed
      everything.
- [x] Make `/help` the index of everything the bot does, and give the failed parse
      its correction back. Raised by the user 2026-08-08 after `/help` shipped:
      it named only `/undo` and the dashboard, so **`/household`, `/invite`,
      `/remove` and `/transfer` were undiscoverable** — `/household` named
      `/invite` only in its *solo* reply (vanishing exactly when a second member
      makes it useful), and `/remove`/`/transfer` described themselves only inside
      their own error paths, which need the command to reach.
      Three parts: `HELP_TEXT` split out as the manual and listing every command;
      `REPHRASE_PROMPT` returned to a short correction that names the *amount* as
      the problem (one string serving both jobs had cost the correction its point);
      and a `/household` footer naming `/invite`, `/remove` and `/transfer` at the
      one moment they mean anything, **gated on the viewer** so a member is never
      offered a command that will refuse them.
      Deliberately **no `web_app` button** on help or welcome, though Telegram
      supports one in private chats: help exists to teach the permanent way in, and
      a shortcut on a message nobody revisits teaches "type /help first".
      The dashboard line names the menu button — `Dashboard` — exactly as it reads
      on screen. That label lives in BotFather and **nothing in the repo could see
      it**, so it is now recorded in `docs/DEPLOYMENT.md`; a rename there must
      change the text here too, and no test will catch it.
- [x] Add `/invite_signup <label>` — the operator admits a tester with their own
      household (§16). The `signup` grant already existed in the schema and in
      `consume_invite`, but nothing could **issue** one: `/invite` mints the
      `household` kind, so the only way to onboard a tester who is *not* joining
      your household was an INSERT by hand. Gated on `ADMIN_TELEGRAM_IDS`, not on
      owning a household — the gate `/invite` uses would let every admitted tester
      admit more, which is a closed beta that opens itself. Fails closed: unset
      admits nobody. **Not registered in BotFather** on purpose — the "/" menu is
      the user-facing manual, and an operator command listed there is an invitation
      to try it and be refused; it is likewise absent from `HELP_TEXT`.
- [ ] `[human]` Set `ADMIN_TELEGRAM_IDS` to your own Telegram user id on
      `kanakko-web` in Dokploy (`scripts/push-env.py` pushes it from `.env`), then
      redeploy. Until it is set, `/invite_signup` refuses everyone — including you.
      Get the id by sending any message and reading `from.id` from
      `getUpdates`, or from `@userinfobot`. Note that `push-env.py` **replaces**
      the whole application env, so anything set only in the Dokploy UI is dropped
      on the next push — that is why the optional keys now ride along from `.env`.
- [ ] `[human]` Set up the BotFather surfaces — no code, and the bot has none of
      them beyond the menu button. **`docs/DEPLOYMENT.md` now carries the exact
      command list to paste**, and it must stay in step with
      `handlers.py::HELP_TEXT` or the "/" menu and `/help` disagree about what the
      bot can do. Set the **description**, shown on the empty chat screen *before*
      a new user presses Start — the first sentence any tester reads, and the
      highest-leverage text in the product. Set the **About** text on the profile.
      The Mini App menu button is already registered (Phase 4) and already labelled
      `Dashboard`, so there is nothing to rename — just confirm the label still
      matches what `HELP_TEXT` tells people to tap. Verify from a Telegram account
      that has never opened the bot: the pre-Start screen cannot be seen any other
      way, and that same account is what 6a.7-6a.10 in `docs/TESTING.md` need.

---

## Phase 10 — Accounts, transfers, and the things that hang off them

Implements [`docs/DECISIONS.md`](docs/DECISIONS.md) **§18**. Read it first — it
settles every question this phase would otherwise re-argue, including the ones
deliberately answered "no" (portfolio value, per-member privacy, statement
upload).

**Ordered so the model lands before everything that sits on it.** 10.1–10.4 are
the account model; nothing after them makes sense without it, and retrofitting an
account onto a ledger of untagged rows means guessing where each old row came
from. Do not reorder.

**Four things §18 assumes that the code does not currently provide.** Walked on
2026-08-12 before any of this was written, listed once rather than rediscovered
per task:

1. **`transactions.type` is a two-value CHECK** (`expense` | `income`,
   `migrations/001_init.sql:18`) and the amount is positive with direction in the
   type. A transfer needs two account references and belongs to neither
   direction, so this is a schema change, not a new enum value alone.
2. **The parse schema's `category` enum is static** — `categories.schema_enum()`
   feeds `build_request` once (§11). The account enum cannot be: it is per
   household, so `build_request` must take the caller's accounts. This is the
   §18 departure from §11 and the one place the two specs deliberately differ.
3. **Every report filters `type = 'expense'`** (`db/reports.py:37,66,78`). Each
   of those is a place a transfer would silently become spending, and a refund
   would silently not reduce it.
4. **The audit action CHECK is closed** — `confirm` | `undo` | `delete` |
   `recategorise` (`migrations/003_transaction_events.sql`). Edit, refund,
   transfer and adjustment each mutate money and each needs a value there, or
   §17's trail has holes exactly where the new money paths are.

### The account model — nothing else works until this is done

- [x] Add `migrations/009_accounts.sql`: `accounts` (household_id, owner
      user_id, `kind` CHECK in `spending`/`credit`/`locked`/`external`, name,
      `opening_balance NUMERIC(12,2)` — signed, since a `credit` account's is
      what is owed — `is_default BOOLEAN`, created_at, deleted_at). One default
      per household enforced by a partial UNIQUE index, not by application code.
      Every household gets a `external` account at creation — §18 makes it the
      counterparty for opening balances and adjustments, so it is structural, not
      optional. No transaction changes yet; this task is the table and its
      constraints, proven against a real server.
- [x] Add `account_id` to `transactions`, nullable, with the backfill: every
      existing row belongs to its household's default account. Then `SET NOT
      NULL` in the same idempotent style as 007/008 — a three-step migration
      because the column cannot be NOT NULL before the backfill runs. `db/`
      writes and reads keep working unchanged; this task must not alter a single
      report number, and the check that proves it is a before/after comparison of
      `month_summary` across the migration.
      Split — did the nullable + backfill half (migration `010`): each existing
      household gets a default `spending` account ('Cash'), every row is homed to
      it, the view is recreated (`SELECT *` froze the list at 007), report numbers
      unchanged (before/after `month_summary` check in `test_migrate.py`). **`SET
      NOT NULL` is deferred**, exactly as 007 deferred it to 008: enforcing it now
      breaks the first confirm of every household minted *after* the migration,
      because no household-creation path yet mints a default account and
      `confirm_pending` doesn't stamp `account_id`. That wiring is the onboarding
      task below ("Everyone gets a default `spending` account"); NOT NULL lands
      once it does.
- [ ] Add the `transfer` type: extend the `type` CHECK, add
      `from_account_id`/`to_account_id` (both NULL except on transfers, and both
      NOT NULL when the type *is* transfer — a CHECK, so the invariant is
      structural). **Exclude transfers from every expense and income total**
      (`db/reports.py:37,66,78`). The guard: a transfer of ₹5,000 between two of
      a household's accounts must move neither the spend nor the income figure,
      and it must fail if any one of those three filters is missed — assert the
      totals, not the SQL text (CLAUDE.md).
- [ ] Derive balances: `opening_balance + inflows − outflows` per account, as a
      view or one query. **Never a stored running total** (§18) — a second source
      of truth that drifts silently is the one failure a money app cannot
      survive. `credit` accounts read as what is owed, so the sign convention is
      part of this task and needs its own check, not a comment.

### Onboarding, parsing, and the confirm card

- [ ] Ask for accounts at onboarding: which kinds they have, and an opening
      balance for each. **A `credit` account is asked "how much do you currently
      owe", not "what is in it"** (§18) — same column, different question, and a
      card asking the wrong one is nonsense on screen. Everyone gets a default
      `spending` account whether or not they answer, so an abandoned onboarding
      still leaves a working bot.
- [ ] Enforce `account_id` NOT NULL — the write-wiring half split off from the
      "Add `account_id` to `transactions`" task above, deferred like 007→008.
      Prereq (the task above): every household-creation path
      (`create_household_of_one`, member re-homing) mints a default account and
      `confirm_pending` stamps `account_id` from it, so no new insert omits the
      column. Then a migration mirrors 008's single `SET NOT NULL`. Check: a
      household minted after the migration can still record its first confirm, and
      a NULL `account_id` insert is refused.
- [ ] Make the parse schema's account enum per-request: `build_request` takes the
      household's accounts and emits them as the `account` enum. §18 says this
      departs from §11 deliberately — the list still comes from the accounts
      table and never from a literal. A household with one account must produce
      the same prompt cost and the same behaviour as today.
- [ ] Show the account on the confirm card with one tap to change it — the §3/§5
      pattern, never a question. **The daily path must not gain a tap:** "spent
      500 on tea" with one account still confirms in one tap, and that is the
      check. Reuse the category keyboard's shape rather than inventing a second
      chooser.
- [ ] Teach the parse prompt the account vocabulary: "swiped", "on card", "paid
      cash", "UPI" (→ the bank account, **never its own account** — §18: UPI is a
      rail, not a pool), "put 5000 in SIP", "FD 1 lakh", "paid chit". Wrong-pool
      is a new error class, so the confirm card showing the account is what makes
      it recoverable.

### The things accounts make possible

- [ ] Credit card semantics end to end: a swipe is an expense on the `credit`
      account and increases what is owed; paying the bill is a transfer from a
      `spending` account and **is not spending a second time**. The guard is the
      double-count itself — swipe ₹2,000, pay the bill, and the month's spending
      must read ₹2,000, not ₹4,000.
- [ ] Investment accounts: auto-create a `locked` on first mention ("put 5000 in SIP" →
      "new savings account 'SIP'?", one tap), contributions and maturities as transfers,
      and a per-account total of what went in and what came back. **No market value,
      ever** (§18) — it reports contributions, which are facts.
- [ ] Editing a row in the dashboard: amount, date, note, category, account.
      §13 built the recent-transactions list for exactly this ("correcting older
      entries") and §16 already scopes it — only the member who entered a row may
      edit it. Add `edit` to the audit action CHECK with before/after (§17), or a
      money-changing operation has no trail.
- [ ] Refunds, linked and partial (§18): a refund references the transaction it
      refunds, may be less than the original, and **the sum of refunds against a
      transaction can never exceed it** — cause that overflow and watch the guard
      go red before believing it. It reduces the original's category total, never
      income. `refund 500` lists recent candidates, amount-matched first, and one
      tap picks the row — reuse `/remove`'s chooser keyboard.
- [ ] Remove `Refund` from `INCOME_CATEGORIES` (§11, §18) — it inflates income
      and the previous task replaces it. Existing rows carrying it need a
      decision recorded in the commit, not a silent rewrite.
- [ ] Recurring rules for auto-debits: amount, category, account, day of month,
      active. **On the day the cron sends the ordinary confirm card** — Confirm /
      Change amount / Skip — never a silent insert (§18: a chit instalment
      changes monthly, so a fixed auto-entry is wrong nearly every time). Reuse
      the §4 confirm path rather than building a second write path. Pause and
      delete live in the dashboard, consistent with the edit task above.
- [ ] The reconcile nudge (§18): weekly, per account, "I think your Bank has
      ₹42,300 — what does your bank say?" A different figure writes a **visible
      adjustment row** against the `external` account. The guard is that the
      adjustment appears in the ledger and in the audit trail — a silent
      correction is the failure mode, so a test that only checks the balance
      afterwards would pass on the broken version.

### After the code

- [ ] Add a Phase 10 section to `docs/TESTING.md` — the double-count case (swipe
      then pay the bill), an FD round trip, a partial refund and an attempt to
      over-refund, the daily path still being one tap, and a reconcile that
      leaves a visible row.
- [ ] `[human]` Re-run the manual QA that Phase 10 touches, and set opening
      balances on your own accounts before demoing.

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
