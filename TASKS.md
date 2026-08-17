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
- [x] Add the `transfer` type: extend the `type` CHECK, add
      `from_account_id`/`to_account_id` (both NULL except on transfers, and both
      NOT NULL when the type *is* transfer — a CHECK, so the invariant is
      structural). **Exclude transfers from every expense and income total**
      (`db/reports.py:37,66,78`). The guard: a transfer of ₹5,000 between two of
      a household's accounts must move neither the spend nor the income figure,
      and it must fail if any one of those three filters is missed — assert the
      totals, not the SQL text (CLAUDE.md).
      Done — migration `011_transfer_type.sql`: widens the `type` CHECK to
      `('expense','income','transfer')`, adds the two nullable account-endpoint
      columns with a structural CHECK (both set iff transfer), recreates the view.
      **`db/reports.py` needed no change**: its sums use positive `type = 'expense'`
      / `type = 'income'` FILTERs, so a `transfer` row is invisible to both totals
      and the category breakdown by construction. The guard
      (`test_transfer_is_excluded_from_spending_and_income_totals`) seeds an
      expense, income, and a ₹5,000 transfer, asserts `day_summary`/`month_summary`
      read back exactly the expense and income, and also exercises the CHECK
      invariant. Verified it reddens by broadening the day filter to
      `type <> 'income'`.
- [x] Derive balances: `opening_balance + inflows − outflows` per account, as a
      view or one query. **Never a stored running total** (§18) — a second source
      of truth that drifts silently is the one failure a money app cannot
      survive. `credit` accounts read as what is owed, so the sign convention is
      part of this task and needs its own check, not a comment.
      Done: `db.account_balances(conn, user_id)` — one query, no stored column,
      reads `active_transactions` (§6) so soft-deleted rows never count, scoped to
      the caller's household (§16). Income/expense sum by `account_id`, transfers
      by their endpoint columns (a transfer touches two accounts, never a total).
      The sign convention lives in the query's `CASE`: a `credit` account's asset
      balance (negative in debt) is negated so it reads as what is *owed*. Storage
      is asset-convention — a credit debt is a negative `opening_balance`, which is
      what 009's "can be negative" already permits (the onboarding task will store
      it that way). `tests/test_accounts.py` seeds opening balances, income,
      expense, transfers, a soft-deleted row and the credit account, asserts each
      derived balance, and reddens if the credit negation is dropped.

### Onboarding, parsing, and the confirm card

- [x] Ask for accounts at onboarding: which kinds they have, and an opening
      balance for each. **A `credit` account is asked "how much do you currently
      owe", not "what is in it"** (§18) — same column, different question, and a
      card asking the wrong one is nonsense on screen. Everyone gets a default
      `spending` account whether or not they answer, so an abandoned onboarding
      still leaves a working bot.
      Done as a command, not an interactive wizard — a card offering kind buttons
      still needs a free-text amount reply, which means new per-user conversation
      state either way, so `/account <kind> <amount>` (`credit`/`locked`, the two
      askable kinds — `spending` is minted automatically and `external` is
      structural) reuses the `/invite`-style single-command shape the bot already
      has, with no new state machine. `WELCOME` names it right after describing the
      default account, so onboarding surfaces it without a second message or a
      forked welcome text; `HELP_TEXT` lists it permanently. `set_account_opening_balance`
      is where "same column, different question" lives: `credit`'s reported amount
      is negated on the way into `opening_balance`, `locked`'s is stored as-is — one
      account per kind per household, so a repeat call corrects a typo instead of
      minting a duplicate `Card`. The other half — `create_household_of_one` now
      calls `create_default_accounts`, minting the default `spending` ('Bank') and
      the structural `external` account the moment a household exists, and
      `confirm_pending` stamps every new transaction's `account_id` from that
      default — is the prerequisite the next task already names. Checks: household
      creation mints exactly `{spending, external}` with no onboarding answer;
      `set_account_opening_balance`'s credit negation and repeat-call update
      (verified red without the fix); `confirm_pending` stamping the default
      `account_id` (verified red without the wiring); `/account`'s usage, bad-kind
      and bad-amount refusals; the webhook routes `/account` to `handle_account` and
      never meters it (verified red without the routing branch).
- [x] Enforce `account_id` NOT NULL — the write-wiring half split off from the
      "Add `account_id` to `transactions`" task above, deferred like 007→008.
      Prereq (the task above): every household-creation path
      (`create_household_of_one`, member re-homing) mints a default account and
      `confirm_pending` stamps `account_id` from it, so no new insert omits the
      column. Then a migration mirrors 008's single `SET NOT NULL`. Check: a
      household minted after the migration can still record its first confirm, and
      a NULL `account_id` insert is refused.
      **Settle the transfer case in this task**: a `transfer` names two accounts
      and no single one, so blanket NOT NULL forces it to pick arbitrarily. Either
      the constraint exempts transfers (a CHECK: `account_id` NOT NULL unless the
      type is transfer) or a transfer stamps its `from_account_id`. Decide and say
      which in the migration comment — an arbitrary pick made silently is a number
      that looks right and answers the wrong question.
      Done: `migrations/012_transactions_account_not_null.sql` adds a CHECK
      (`account_id IS NOT NULL OR type = 'transfer'`) rather than a blanket
      `SET NOT NULL` — a transfer keeps `account_id` NULL and names its two ends via
      `from_account_id`/`to_account_id` (011) instead. The prereq already held: both
      household-creation paths route through `create_household_of_one`, which always
      mints the default `spending` account, and `confirm_pending` was already
      stamping every new row from it. The real work was the test fixtures: the
      shared `household_of()` test helper (`tests/conftest.py`) predated Phase 10 and
      hand-rolled a household with no accounts, so ~20 raw-SQL transaction-insert call
      sites across 6 test files would have started failing the new CHECK. Fixed at
      the root: `household_of()` now delegates to `create_household_of_one` (so every
      test household gets its default account, same as production), and a new
      `default_account_of()` test helper looks it up for the handful of raw inserts
      that need to stamp `account_id` explicitly. `test_migrate.py` also gained one
      test (`test_transactions_account_id_is_required_except_for_transfers`) —
      verified red without the CHECK, green with it restored. `uv run pytest` →
      321 passed.
- [x] Show transfers in the dashboard's recent list. `recent_transactions` returns
      every live row regardless of type, so a transfer will surface there the
      moment one can be written — and `_category_select` will render it an empty
      dropdown, because `CATEGORIES_BY_TYPE` has no `transfer` key. It degrades
      rather than crashes today, which is why this needs a task rather than a
      finding: a transfer should read as "Bank → SIP", not as a category-less
      expense. While there: the hero panel's **"Balance" stat is `income −
      expenses`**, a flow, and now that `account_balances` exists that name means
      something else on the same screen. Rename it or show the real thing.
      Done: `recent_transactions` (`kanakko/db/reports.py`) now `LEFT JOIN`s
      `accounts` twice (`from_account_id`/`to_account_id`) and returns their
      names alongside the existing columns — `NULL` for every type but
      `transfer`. `recent_list` (`kanakko/webapp/render.py`) renders a `transfer`
      row's two account names ("Bank → SIP") in place of the category `<select>`,
      and drops the −/+ sign and income tint, since a transfer is neither
      spending nor income (§18) — the empty-dropdown/green-`+` degradation is
      gone, not just hidden. Chose the smaller of the two "Balance" fixes: renamed
      the hero panel's flow stat to "Net" rather than wiring `account_balances`
      (a real stock figure) into the dashboard, which is a bigger, undecided
      surface — no task asks for account balances on this screen yet, and adding
      one silently would be scope creep. Two new tests in `tests/test_webapp.py`
      cover the transfer row (account names shown, no dropdown, no sign/tint) —
      verified red against the pre-fix rendering, green restored. `uv run pytest`
      → 323 passed.
- [x] Make the parse schema's account enum per-request: `build_request` takes the
      household's accounts and emits them as the `account` enum. §18 says this
      departs from §11 deliberately — the list still comes from the accounts
      table and never from a literal. A household with one account must produce
      the same prompt cost and the same behaviour as today.
      Done: `db.list_accounts(conn, user_id)` (`kanakko/db/accounts.py`) —
      household-scoped account names, `external` excluded (structural, never a
      natural-language target), default first; empty for a user with no
      household yet, which is what keeps the schema unchanged for that edge
      case. `parse_schema(accounts=None)` adds a nullable `account` `anyOf`
      enum (§18: null → the default account, same nullable pattern as
      `category`) only when `accounts` is truthy — omitted or empty leaves the
      schema exactly as it was, verified by
      `test_a_household_with_one_account_behaves_like_no_accounts_at_all` and
      `test_account_enum_is_absent_without_accounts`. `build_request`/`call`
      thread `accounts` straight through. `Transaction` gains `account: str |
      None = None`; since the closed set is per household rather than a module
      constant, it is checked via Pydantic validation context
      (`parse_message(message, accounts)` passes `context={"accounts":
      accounts}`) instead of a class-level set like `category`'s. `handlers.
      handle_text` calls `list_accounts` and threads the result into both the
      trace's `build_request` call and `parse_message`. Four guards verified
      red-without-fix, green-with-fix: the `external` exclusion, the
      out-of-set account validator, and the `handle_text` wiring. `uv run
      pytest` → 334 passed (11 new).
- [x] Show the account on the confirm card with one tap to change it — the §3/§5
      pattern, never a question. **The daily path must not gain a tap:** "spent
      500 on tea" with one account still confirms in one tap, and that is the
      check. Reuse the category keyboard's shape rather than inventing a second
      chooser.
      Done: `confirm_card` (`kanakko/confirm.py`) takes the caller's `accounts`
      and, only when `len(accounts) > 1` (the same gate `parse_schema` already
      uses, §18: "accounts become visible only when a second one exists"),
      inserts an `Account: <name>` line (`txn.account` or the default,
      `accounts[0]`, when null) and appends `acct:<name>` buttons via a new
      `account_keyboard` — `categories.keyboard`'s exact two-per-row shape, not
      a second chooser. A one/no-account household gets byte-for-byte the
      pre-accounts card. `handle_text`/`handle_category` thread `accounts`
      through; a new `handle_account_choice` (routed on `ACCOUNT_PREFIX =
      "acct:"` in `app.py`) mirrors `handle_category`: resolves the tapping
      user's own household accounts first (the closed set is per household, not
      module-level like category's), ignores a tap naming an account outside
      it, and re-renders the card via a new `db.set_pending_account`.
      Also closed the gap that made the picker cosmetic: `confirm_pending`
      previously always stamped the household's *default* account regardless of
      `txn.account`. Its account lookup now resolves the pending row's chosen
      account by name within the household, falling back to the default only
      when `txn.account` is null (one `coalesce`d query, no extra round trip).
      Three guards verified red-without-fix, green-with-fix: the `len(accounts)
      > 1` display gate, `confirm_pending` honouring the chosen account instead
      of always the default, and `handle_account_choice` rejecting an
      out-of-household account name. `uv run pytest` → 343 passed (8 new).
- [x] Teach the parse prompt the account vocabulary: "swiped", "on card", "paid
      cash", "UPI" (→ the bank account, **never its own account** — §18: UPI is a
      rail, not a pool), "put 5000 in SIP", "FD 1 lakh", "paid chit". Wrong-pool
      is a new error class, so the confirm card showing the account is what makes
      it recoverable.
      Done: `_ACCOUNT_GUIDANCE` (`kanakko/parse.py`) appended to the system prompt
      in `build_request`, gated on the same `accounts and len(accounts) > 1` check
      `parse_schema`'s `account` enum already uses — a single-account household's
      prompt is byte-for-byte unchanged, no added cost. Teaches the vocabulary by
      account *kind* ("swiped"/"on card" → credit, "UPI"/"paid cash" → the
      everyday spending account, "SIP"/"FD"/"chit" → the locked account) rather
      than naming a literal account — the enum is still the household's own
      names. Explicitly tells the model UPI is a rail, never an account of its
      own. A wrong guess isn't a failure: the confirm card shows the account with
      one tap to fix it. `test_account_vocabulary_guidance_appears_only_with_a_real_account_choice`
      (`tests/test_parse.py`) verified red without the gate wired in, green with
      it restored. `uv run pytest` → 344 passed (1 new).

### The things accounts make possible

- [x] Credit card semantics end to end: a swipe is an expense on the `credit`
      account and increases what is owed; paying the bill is a transfer from a
      `spending` account and **is not spending a second time**. The guard is the
      double-count itself — swipe ₹2,000, pay the bill, and the month's spending
      must read ₹2,000, not ₹4,000.
      Done: the swipe half already worked (an ordinary expense on the `credit`
      account, existing account machinery) — the gap was that nothing could ever
      *produce* a transfer, so a bill payment had no way to avoid being parsed as
      a second expense. `parse_schema`'s `type` enum gains `"transfer"` and two
      new `from_account`/`to_account` fields, all three under the same
      `len(accounts) > 1` gate the `account` enum already uses (§18: a transfer
      needs two accounts to move money between, so it's meaningless without a
      real choice) — a single-account household's schema is unchanged.
      `_ACCOUNT_GUIDANCE` teaches "paid the credit card bill"/"paid off my card"
      as `type: transfer`, `spending → credit`, `category: null`. `Transaction`
      gains `from_account`/`to_account` (validated against the household's own
      accounts, same closed-set validator as `account`) and a model-level guard
      mirroring migration 011's CHECK: a transfer names both ends and no other
      type names either, and the two ends must differ. `confirm_pending`
      (`kanakko/db/pending.py`) gains the write-side branch: a transfer resolves
      `from_account_id`/`to_account_id` by name and leaves `account_id`/`category`
      NULL, instead of falling back to the household default. `confirm_card` and
      `settled_card` (`kanakko/confirm.py`) render a transfer as "Bank → Card"
      with no category line, no category buttons, no account picker — just
      Confirm/Cancel; `handle_text`'s null-category gate excludes `type ==
      "transfer"` so a bill payment never hits the category picker (which has no
      `"transfer"` entry to build buttons from). Six guards verified
      red-without-fix, green-with-fix: the schema gate, the transfer-shape model
      validator (missing end, self-transfer, a non-transfer carrying an
      endpoint), `confirm_pending`'s transfer branch (removing it trips 011's
      CHECK), the named double-count guard itself (swipe ₹2,000 + pay the bill →
      `month_summary` reads exactly ₹2,000), and `handle_text`'s picker exclusion.
      `uv run pytest` → 354 passed (10 new).
- [x] Investment accounts: auto-create a `locked` on first mention ("put 5000 in SIP" →
      "new savings account 'SIP'?", one tap), contributions and maturities as transfers.
      **Split off the per-account total below** — this bullet bundled three things
      and the total is a genuinely separate, unstarted piece (its own report, no
      existing query to extend), not a prerequisite of the other two.
      Done: the auto-create gap was schema-shaped, not application logic — a pool
      named for the first time ("SIP") can't be a `to_account` enum value (it isn't
      in `accounts` yet), so the model has no way to say where the money went.
      `parse_schema`'s `len(accounts) > 1` gate (unchanged — same gate as every
      other §18 field, so a single-account household's schema stays byte-for-byte
      `parse_schema()`, still proven by the existing pinned test) now also adds
      `new_locked_account` (free text, no enum — the escape valve a closed set
      can't offer), and `_ACCOUNT_GUIDANCE` teaches it: "put 5000 in SIP" is
      `type` "transfer", `from_account` the spending account, `to_account` null,
      `new_locked_account` "SIP". `Transaction`'s model validator extends 011's
      mirrored CHECK: a new-pool transfer needs `from_account` and *no*
      `to_account` (the field replaces it), everything else is unchanged.
      `confirm_card` leads with the question the task names — 'New savings
      account "SIP"?' — over the same Confirm/Cancel buttons, no new callback
      route. `confirm_pending`'s transfer branch get-or-creates the `locked`
      account (SELECT before INSERT, so a stale card racing a same-named pool
      reuses it rather than duplicating it) and resolves it as `to_account_id`;
      the settled receipt names it via `txn.to_account or txn.new_locked_account`.
      **Maturities needed no code**: once a pool has been mentioned once, its name
      is in `accounts` and an ordinary transfer (`from_account` the pool,
      `to_account` the spending account) already works — this task was only ever
      about the pool that doesn't exist yet. Five guards verified red-without-fix,
      green-with-fix: the schema gate, the guidance text, the Transaction
      validator (`extra_forbidden` without the field), the confirm-card question
      (rendered "Bank → None" without it), and `confirm_pending` minting the
      account (a `CheckViolation` without it — a silent NULL `to_account_id` on a
      transfer is refused at the DB, not just wrong). `uv run pytest` → 364 passed
      (10 new).
- [x] Investment accounts: a per-account total of what a `locked` account has
      received (contributions) and paid out (maturities) — split off the bullet
      above. **No market value, ever** (§18): this reports two sums from the
      ledger, not a balance and not what the pool is worth today — `locked`
      already has a derived balance (`db.account_balances`) but no read shows the
      gross in/out `docs/DECISIONS.md` calls the fact worth keeping ("it knows
      what you put in and what came back, both of which are facts"). Undecided
      and left to that task: where it surfaces — a bot command (`/account`
      already exists as a light command surface) or a dashboard section.
      **Resolved:** the bot command, since `account_balances` itself has zero
      consumers today — no account UI exists anywhere yet, so a dashboard section
      would be new surface on top of new surface. `db.locked_account_totals`
      sums `transfer` rows by endpoint per `locked` account (excluding
      `opening_balance` — a starting position, not a ledger event) and
      `/account <name>` — one bare word, previously an unused shape that fell
      through to the usage hint — reports it case-insensitively: "SIP — put in
      ₹7,000.00, got back ₹3,000.00." `HELP_TEXT` documents the new form. Three
      guards verified red-without-fix: opening balance folded into the sum, and
      an exact-case name match, each redden their test.
- [x] Editing a row in the dashboard: amount, date, note, category, account.
      §13 built the recent-transactions list for exactly this ("correcting older
      entries") and §16 already scopes it — only the member who entered a row may
      edit it. Add `edit` to the audit action CHECK with before/after (§17), or a
      money-changing operation has no trail.
      **Resolved:** category already had its own control (task 101); this adds
      amount, date, note, and account. One generic `db.edit_transaction_field`
      (a closed `EDITABLE_TRANSACTION_FIELDS` column whitelist feeds an f-string,
      never attacker input) and one `POST /app/edit` route cover all four rather
      than four near-duplicate functions/routes, mirroring how `field` is
      dispatched client-side too. `account_id` gets two extra rules an f-string
      column name can't express: refused on a `transfer` row (it names two ends,
      not one, §18) and the new id must resolve to *this* household's own live,
      non-`external` accounts (an `EXISTS` check) — both verified red-without-fix,
      the second is a real cross-household relocation a forged id would otherwise
      cause. Migration 013 widens the `transaction_events.action` CHECK to admit
      `'edit'`; the guard for it is the audit-row test itself, which throws a
      `CheckViolation` if the migration is reverted (verified). UI: a small
      pencil toggle reveals a hidden per-row panel (native `<input type=date>`/
      `type=number`, no picker library, §7) rather than restyling the always-
      visible summary line, so none of that line's existing layout tests moved.
      15 new tests, all guards verified red-without-fix. Split `db/reports.py`
      (reads) from a new `db/edits.py` (the three per-row mutations) and
      `webapp/render.py` (period panels) from a new `webapp/recent.py` (the list)
      to stay under CLAUDE.md's 300-line guideline, which this task's own code
      would otherwise have pushed both past.
- [x] `app.py` is 497 lines, well past CLAUDE.md's 300-line guideline (it was
      already 420 before task 974 added the `/app/edit` route). Unlike the two
      splits task 974 made, this one is not free: the Mini App routes
      (`/app/data`, `/app/delete`, `/app/category`, `/app/edit`,
      `authenticated_user`, `permitted_user`) are the obvious seam to move into
      `kanakko/webapp/routes.py`, but `tests/test_webapp.py` monkeypatches
      `kanakko.app.connect` directly (`monkeypatch.setattr(app_module,
      "connect", ...)`) — moving the routes without also re-threading that
      indirection would make the patch silently stop applying and every mocked
      test would try to hit a real `DATABASE_URL`. Needs that wired through
      first (an `APIRouter`, or a passed-in `connect`), not just a file move.
      Resolved by moving `connect` (and every dependency the four routes use)
      into `kanakko/webapp/routes.py` alongside the routes themselves, as an
      `APIRouter` included by `app.py`. `connect` is now looked up unqualified
      from *that* module at call time, so `tests/test_webapp.py`'s
      `monkeypatch.setattr(app_module, "connect", ...)` keeps working once its
      one import line points `app_module` at `kanakko.webapp.routes` instead of
      `kanakko.app` — no per-test-site change needed. Verified the indirection
      actually matters, not just plausible: reverted that one import line,
      reran `tests/test_webapp.py`, and 19 of the mocked tests failed with
      `RuntimeError: DATABASE_URL is not set` (the patch silently stopped
      applying, exactly as this task predicted) — then restored it, 71 passed.
      `app.py` is now 222 lines; `kanakko/webapp/routes.py` is 299.
- [x] Refunds, linked and partial (§18) — schema, guard and write path (split
      1/2). Split the same way 010/012 split account_id's nullable-then-enforce
      pair: this half is the money-correctness core (a stub here would be a
      silent ledger bug, not a deferrable UI gap); the bot-facing `refund <amount>`
      chooser and the dashboard "Refund" action are the next task (split 2/2,
      immediately below) — pure UX wiring on top of what this migration and write
      path already make correct.
      Migration 014 widens `transactions_type_check` to add `refund`, adds
      `refund_of_txn_id` (structural CHECK: set iff `type = 'refund'`, mirroring
      011's transfer-accounts check), and adds this schema's first cross-row
      guard — every prior guard here is a CHECK or a partial UNIQUE INDEX, neither
      of which can aggregate across rows, so **the sum-never-exceeds rule is a
      `BEFORE INSERT OR UPDATE` trigger**, not a CHECK. Verified red-without-fix:
      commented out the `CREATE TRIGGER` statement, reran
      `test_refund_sum_cannot_exceed_the_original`, watched
      `DID NOT RAISE RaiseException`, restored it, green again.
      `kanakko.db.refunds.create_refund` is the write path: household-scoped (not
      user-scoped — §16's shared ledger means any member may refund any member's
      expense, same reach as `recent_transactions`/`month_summary`), refuses
      anything but a live `expense` row, and copies the original's `account_id`/
      `category` onto the refund row — the account copy is what returns the money
      to the account it was spent from, the category copy is what lets
      `reports.py` net a refund against its category with a plain `GROUP BY`
      instead of joining back to the original in every report query. Wired the
      net-out into `day_summary`, `month_summary` (both the total and the
      per-category breakdown, dropping a category that nets to zero rather than
      showing a `₹0.00` row) and `account_balances` (a refund is an inflow,
      alongside income) — three separate query paths, each could have silently
      forgotten the new type, so each has its own assertion in
      `test_refund_reduces_category_and_account_but_never_income`. `transaction_events`
      gets a `refund` action value (013's two-liner pattern). Never touches
      `INCOME_CATEGORIES` — that is the very next task below, a separate
      decision about existing rows.
- [x] Refunds — the bot-facing UX (§18, split 2/3, was "split 2/2" — the
      Mini App dashboard action is carved off below as its own task; it's UX
      wiring on a separate surface with its own 300-line file-size constraint,
      not the money-correctness core). `refund <amount>` (`handlers._is_refund`
      — deliberately not a slash command, so it gets its own predicate in
      `app.py`'s `is_parse` exclusion list, same as `/undo`/`/remove`) lists
      live, not-fully-refunded expenses via `db.refunds.refund_candidates` —
      a sibling query to `recent_transactions`, `amount`-matched first, both
      sides of its join reading `active_transactions` (§6) rather than the base
      table (`test_read_paths.py`'s existing bypass guard caught the first
      draft joining raw `transactions` for the refund side — the guard failing
      for the reason it exists, not a new check). One tap
      (`handlers.handle_refund_choice`) picks a candidate and calls
      `create_refund`; the spec's "reuse `/remove`'s chooser keyboard" is stale
      (that's a static two-button Keep/Delete, not an N-candidate list) —
      reused the actual shape instead, `categories.py:keyboard`/
      `confirm.py:account_keyboard`'s N-buttons-from-a-list-one-prefix pattern,
      one button per row since a candidate's label (amount, category, date) is
      longer than a category name. The trigger's `RaiseException` on an
      over-limit tap (a second member refunding the same expense between the
      list and the tap, or a stale button) is caught and answered rather than
      left to 500 — `test_handle_refund_choice_over_limit_is_caught_not_500`
      also asserts the connection is still usable afterward, since `create_refund`
      takes its own savepoint (`conn.transaction()`), not the whole transaction.
      `refund` (bare word) is now in `HELP_TEXT` and `test_help.py`'s
      undiscoverable-command guard.
- [x] Refunds — the Mini App dashboard action (§18, split 3/3): a "Refund"
      toggle on each `expense` row (only an expense is refundable — `create_refund`
      accepts nothing else), alongside the existing Delete/category-select/edit
      controls, revealing a hidden panel (`kanakko.webapp.recent._refund_panel`)
      with an amount input defaulting to the row's full amount and its own
      explicit submit button — unlike the edit fields, which auto-save on
      `change`, a refund *adds* a row rather than overwriting one, so firing on
      blur would create a transaction nobody confirmed; it needs the same
      explicitness the bot's button tap has. Submit POSTs to a new `POST
      /app/refund` route (`{"id": <txn_id>, "amount": <string>}`) that calls
      `db.refunds.create_refund` the same way `handle_refund_choice` does — 24h
      `max_age` (state-mutating, §13), household-scoped not user-scoped (§16,
      matching `create_refund` itself), the trigger's `RaiseException` caught and
      turned into `409` (distinct from the `404` a gone/foreign/non-expense row
      gets, `400` for an unparsable body). `routes.py` was already at 299 lines
      (task 974's own docstring flags it), so a fifth route there would have
      overshot CLAUDE.md's 300-line guideline — went with the sibling-module
      option the task named rather than the mutations/reads split, since it is
      the smaller diff and the existing `routes.py`/`test_webapp.py` monkeypatch
      wiring for the other three mutation routes needed no change:
      `kanakko/webapp/refund.py` is its own `APIRouter`, included alongside
      `webapp_router` in `app.py`, importing `authenticated_user`/`permitted_user`
      from `routes.py` rather than a third copy. Verified the `RaiseException`
      catch is load-bearing, not decorative: temporarily removed it, watched
      `test_refund_route_over_limit_is_409_and_writes_nothing` turn into an
      unhandled 500, restored it.
- [x] Remove `Refund` from `INCOME_CATEGORIES` (§11, §18) — it inflates income
      and the previous task replaces it. Existing rows carrying it need a
      decision recorded in the commit, not a silent rewrite.
      Done: dropped from the tuple in `categories.py`; `test_categories.py`'s
      hand-transcribed `SPEC_INCOME` now notes §18 amends §11 and the change
      was verified red-without-fix (temporarily restored `Refund`, watched all
      three spec/enum/keyboard assertions fail, restored the fix).
      **Decision on existing rows:** left as-is, no data rewrite. The category
      column has no DB CHECK (`001_init.sql:19`, deliberately — "the DB would
      be a second place to edit them"), so old `income`/`Refund` rows stay
      exactly as entered; rewriting them would silently revise a historical
      fact nobody asked to correct. The dashboard's category `<select>`
      (`webapp/recent.py::_category_select`) already degrades an
      out-of-set value to a disabled "Uncategorised" placeholder — the same
      path a null category already takes — so an old `Refund` row is editable
      to a real category in one tap if a member wants to reclassify it, but
      nothing forces that.
- [x] Recurring rules for auto-debits: amount, category, account, day of month,
      active — schema and the household-scoped CRUD (split 1/2). **Split the
      same way 014 split refunds**: the cron that finds a rule due today and
      sends the ordinary confirm card (Confirm / Change amount / Skip, reusing
      §4's machinery rather than a second write path) and the dashboard's
      pause/delete controls are UX wiring on top of what this migration and
      write path already make correct — a genuinely separate task, not a
      deferrable stub, since it needs `jobs/` scheduling and the bot-facing
      Confirm/Change-amount/Skip flow that would make this one commit two
      unrelated things.
      Done: migration `015_recurring_rules.sql` — `recurring_rules`
      (household_id, account_id, category — no CHECK, `categories.py` is the
      one place category strings are declared, CLAUDE.md — amount
      `NUMERIC(12,2) CHECK (amount > 0)`, `day_of_month SMALLINT CHECK
      (day_of_month BETWEEN 1 AND 31)`, active default true, created_by,
      created_at), plus two indexes (the cron's future day-of-month lookup,
      the dashboard's future household list). No `transactions` column yet —
      nothing references a `rule_id`, so a rule can be hard-deleted with
      nothing to orphan; the cron task decides how a written transaction ties
      back to the rule that produced it.
      `kanakko.db.recurring` is the write path: `create_recurring_rule`
      (household-scoped account resolution — refuses a foreign household's
      account, the structural `external` account, and a soft-deleted one, the
      same `EXISTS` shape `edit_transaction_field` uses), `list_recurring_rules`
      (household-scoped, both paused and active, joined to the account name),
      `set_recurring_rule_active` (pause/resume) and `delete_recurring_rule`
      (a real `DELETE`, not §6's soft delete — a rule is a standing
      instruction, not a ledger row) — all household-scoped like
      `accounts.py`/`refunds.py`, not creator-scoped, since §16's shared
      ledger means any member may manage a rule another member set up. No
      caller yet, same as `create_refund` when 014 landed — tests call the
      module directly. Five guards verified red-without-fix, green-with-fix:
      the day_of_month range CHECK, the amount CHECK, the foreign/external/
      soft-deleted account refusal in `create_recurring_rule`, and the
      household-scoping on `set_recurring_rule_active`. `uv run pytest` →
      418 passed (3 new).
- [x] Recurring rules for auto-debits — the dashboard's pause/delete (split
      2/3). Split further off the original "cron send + dashboard pause/delete"
      2/2: the cron send touches the shared confirm path
      (`kanakko.db.pending`/`kanakko.confirm`) and needs a new migration to tie
      a written transaction back to its `rule_id`, and creation's own surface is
      still explicitly undecided (a bot command or a dashboard form) — neither
      belongs in the same commit as a UI that only ever calls the read/pause/
      delete functions 015 already landed and tested directly.
      Done: `kanakko.webapp.recurring` — its own module (`routes.py` is at
      CLAUDE.md's 300-line guideline, the same reason `refund.py` split out),
      `POST /app/recurring/active` (`{id, active}`, calls
      `set_recurring_rule_active`) and `POST /app/recurring/delete` (calls
      `delete_recurring_rule`), both household-scoped (§16, any member may act
      on a rule another member made) and passing a 24h `max_age` (§13). New
      `render.recurring_list` renders each rule ("Food · ₹5,000 · day 5 ·
      Bank") with a pause/resume toggle — `data-active` names the rule's
      *current* state, the client sends the state to move to — and a delete
      button; a paused rule dims (`.paused`) rather than disappearing, since
      resuming it needs it still visible. Wired into `dashboard_html` (new
      `rules` param, `mini_app_data` fetches via `list_recurring_rules`) and
      `shell.py` (CSS + click handlers, same 44px lanes `.del`/`.edit-toggle`
      use). Household-scoping guard verified red-without-fix: temporarily
      dropped the `household_id` join from `set_recurring_rule_active`'s
      UPDATE, watched `test_recurring_active_route_foreign_household_is_404`
      fail, restored it. `uv run pytest` → 428 passed (10 new).
- [x] Recurring rules for auto-debits — the cron send, Confirm/Skip half
      (split 3a/3). Split further off "the cron send (split 3/3)": that task
      bundled three things — the rule↔transaction link, the daily send, and
      "Change amount" — and "Change amount" is a materially separate
      interaction (a new free-text reply mode with its own bot state, not a
      button relabel), not a stub if left for its own commit.
      Done: migration 016 adds nullable `recurring_rule_id` (`ON DELETE SET
      NULL` — a rule's hard delete must not break its own transaction
      history) to both `pending_transactions` and `transactions`, and
      recreates `active_transactions` (§6 — `SELECT *` froze the old column
      list). `db.pending.save_pending`/`confirm_pending` thread it through
      unchanged for every existing caller (`None` by default). New
      `db.recurring.due_rules_today` is the cross-household read (like
      `db.users.all_users`), filtering `active`, the day, and a live account.
      New `kanakko.jobs.recurring` sends the ordinary confirm card via
      `kanakko.confirm.confirm_card`'s new `cancel_label` param — "⏭️ Skip"
      for a cron-sent card — reusing `handle_confirm`/`handle_cancel`
      unchanged, since `callback_data=CANCEL` never moved (§18: "reuses the
      entire §4 confirm machinery"). Its own fan-out loop, not
      `kanakko.jobs.fan_out` (a rule send needs the rule's own data per
      iteration, which that helper's per-user signature has no room for) —
      same failure-isolation shape: one savepoint per rule, a commit before
      raising `DeliveryFailures`. Crontab: daily 08:00 IST, a judgment call —
      §12/§18 name no time. Two guards verified red-without-fix, green-with-
      fix: `recurring_rule_id` surviving Confirm, and the per-rule savepoint
      actually isolating a blocked recipient. `uv run pytest` → 435 passed
      (7 new).
- [x] Recurring rules for auto-debits — "Change amount" on the cron's confirm
      card (split 3b/3), the second of the three buttons §18 specifies
      ("Confirm / Change amount / Skip" — split 3a/3 shipped Confirm/Skip).
      Needs a bot-side "awaiting a free-text amount for pending_id N" state —
      nothing in the webhook today distinguishes "the next text message is a
      new transaction to parse" from "the next text message is a replacement
      amount" — plus re-rendering the card via `edit_message_text` with the
      corrected amount before Confirm is even tappable. Decide where that
      state lives (a pending-row column, since the card being live already
      implies one; not a new table).
      Done: migration `017_pending_awaiting_amount.sql` adds a plain
      `awaiting_amount BOOLEAN NOT NULL DEFAULT false` to `pending_transactions`
      — the column the task named, not a new table. `confirm_card` gains
      `change_amount_button` (only `jobs.recurring` passes it), rendering a
      third row with `callback_data=CHANGE_AMOUNT` between Confirm/Skip and the
      category buttons; `SKIP_LABEL` moved from `jobs/recurring.py` into
      `confirm.py` alongside it, since `handlers.py` now needs the same literal
      to re-render the card. `db.pending` gains the write triple:
      `request_amount_change` (marks the tapped card awaiting, `handle_change_
      amount_request`'s write), `pending_awaiting_amount` (the read `app.py`'s
      webhook runs on *every* `TextMessage`, before any command routing, to
      decide whether the message in hand is a reply or a transaction), and
      `set_pending_amount` (re-validates the reply through `money.parse_amount`
      §9, rewrites only `amount`, clears the flag — scoped to a row still
      `awaiting_amount` so a stray message after the card settled elsewhere
      can't rewrite it). An unparseable reply leaves the row awaiting so the
      user can just retry; Skip on the card (an ordinary `cancel_pending`
      delete) is the escape hatch, since deleting the row deletes the flag with
      it. The amount reply is unmetered and never counts against the daily cap,
      the same free-tap treatment `/undo` gets, since it's not an LLM call.
      Three guards verified red-without-fix, green-with-fix: the cron card
      carrying the "✏️ Change amount" button (temporarily reverted
      `jobs/recurring.py`'s `change_amount_button=True`, watched
      `test_run_sends_a_confirm_card_and_links_the_pending_row_to_the_rule`
      fail), and `set_pending_amount` refusing a row that was never marked
      awaiting (temporarily dropped the `AND awaiting_amount` clause, watched
      `test_set_pending_amount_refuses_a_row_that_is_not_awaiting_one` fail).
      `uv run pytest` → 446 passed (17 new).
- [x] Recurring rules for auto-debits — the creation surface (a bot command in
      the `/account`/`/invite` shape, or a dashboard form). Undecided; belongs
      to its own task, not the schema, dashboard, or cron-send splits above.
      Done: a bot command, the `/account` shape — `/recurring <amount> <day>
      <category> <account>`, e.g. `/recurring 5000 5 Bills & Utilities Bank`.
      `amount` and `day` anchor the front of the command since both are
      unambiguous (a number, then 1-31); `category`+`account` share the rest of
      the text with no delimiter between them, since either is a closed set
      that can be more than one word ("Bills & Utilities", "Kids Fund") — a
      positional split would misparse the moment both sides aren't exactly one
      word. `_match_category_and_account` resolves this by brute force: try
      every `EXPENSE_CATEGORIES` entry (recurring rules are always an expense —
      `jobs.recurring` hardcodes `type="expense"`) as a candidate prefix of the
      trailing text, and check what's left against the caller's own
      `household_accounts` (never a literal, same rule §18 already applies to
      the parse-schema account enum). `handle_recurring` calls
      `db.recurring.create_recurring_rule` (task 1125's CRUD, previously
      uncalled outside its own tests) and always creates the rule active — the
      dashboard's pause/delete (task 1161 split 2/3) is where that changes
      after creation. Two guards verified red-without-fix, green-with-fix: the
      brute-force category/account split (temporarily replaced it with a
      naive first-word-is-category positional split, watched the multi-word
      category test fail with `TypeError: 'NoneType' object is not
      subscriptable`) and the webhook wiring (temporarily dropped the
      `_is_recurring` routing branch in `app.py`, watched the routing/metering
      test fail). `/recurring` also joins the `is_parse` exclusion list (never
      an LLM call, never metered, same as `/account`) and `HELP_TEXT`/
      `test_help.py`'s undiscoverable-command guard. `uv run pytest` → 457
      passed (11 new).
- [x] `handlers.py` split, design pass + slice 1/6 (`/transfer`). Design: the
      shared spine every command module needs — `dispatch`, `TextMessage`/
      `ButtonPress`, `_command_arg`, `handle_start`/`handle_text`/`handle_undo`/
      `handle_help`/`handle_household`, and the confirm-card core
      (`handle_confirm`/`handle_cancel`/`handle_category`/`handle_account_choice`/
      `handle_change_amount_request`/`handle_amount_reply`) — stays in
      `handlers.py`. The six standalone commands (`handle_transfer`,
      `handle_account`, `handle_remove`(+choice), `handle_invite`+
      `handle_invite_signup`, `handle_recurring`, `handle_refund`(+choice)) each
      move to their own module under a new `kanakko/commands/` package, one per
      iteration — mirroring `webapp/refund.py`/`webapp/recurring.py`'s split off
      `webapp/routes.py`, and matching this file's own count: still seven-ish
      pieces, just one file each instead of one 1541-line file. `app.py`'s
      import block and `is_parse` exclusion list move with each handler.
      This slice: `kanakko/commands/transfer.py` (94 lines) — the smallest and
      only one with no `test_webhook.py` dependents, so it proved the pattern at
      the lowest risk. **The gotcha every remaining slice must repeat**: a test
      that `monkeypatch.setattr(handlers, "send_message", ...)` for a handler
      that has moved silently stops applying — the patch binds the unqualified
      name where the call executes, not where the test imports it from (same
      class of bug task 1000's `connect`/`app_module` note already recorded).
      Verified red-without-fix: left `tests/test_transfer.py` patching
      `handlers.send_message` after moving `handle_transfer`, reran, 4 failed
      and 1 errored on a real `InFailedSqlTransaction` (the stub never
      intercepted the send) instead of a clean assertion failure — restargeted
      it at `kanakko.commands.transfer` and all 5 passed. `handlers.py` is now
      1462 lines; `uv run pytest` → 457 passed, `ruff check` clean.
- [x] `handlers.py` split, slice 2/6: `kanakko/commands/account.py`
      (`handle_account` + its three helpers). Retarget
      `tests/test_account_command.py`'s `handlers.send_message` patch and
      `handlers.ACCOUNT_*`/`handlers._is_account` reads at the new module, move
      the `app.py` import, and repeat the red-without-fix check slice 1/6 did.
      Done: moved `ACCOUNT_COMMAND`/`ACCOUNT_USAGE`/`ACCOUNT_BAD_KIND`/
      `ACCOUNT_BAD_AMOUNT`/`ACCOUNT_NO_HOUSEHOLD`/`ACCOUNT_NOT_LOCKED`,
      `_is_account`, `_looks_like_amount`, `_find_locked_account`,
      `_handle_account_query` and `handle_account` verbatim; `app.py` now
      imports `_is_account`/`handle_account` from `kanakko.commands.account`
      (`handle_account_choice` stays in `handlers.py` — it's part of the
      confirm-card core, not this command). Dropped `locked_account_totals`/
      `set_account_opening_balance` from `handlers.py`'s now-stale `db` import.
      `tests/test_account_command.py` retargeted its monkeypatch and every
      `handlers.ACCOUNT_*` read at the new `account_command` module — verified
      red-without-fix by patching `handlers.send_message` again: all 14 tests
      failed with `RuntimeError: TELEGRAM_BOT_TOKEN is not set` (the stub never
      intercepted, the real send path ran), same failure class slice 1/6 found.
      `handlers.py` is now 1286 lines; `uv run pytest` → 457 passed, `ruff
      check` clean.
- [x] `handlers.py` split, slice 3/6: `kanakko/commands/remove.py`
      (`handle_remove` + `handle_remove_choice`). `tests/test_member_removal.py`
      and `tests/test_webhook.py`'s `REMOVE_PREFIX`-routed callback tests both
      patch `handlers` directly — check `test_webhook.py`'s patches
      (`send_message`/`edit_message_text`/`answer_callback_query`) against
      *which* handler each specific test exercises before retargeting, since
      that file spans many commands in one module.
      Done: moved `REMOVE_COMMAND`/`REMOVE_NO_MATCH`/`REMOVE_AMBIGUOUS`/
      `REMOVE_NOT_OWNER`/`REMOVE_OWNER_MUST_TRANSFER`/`REMOVE_NOT_MEMBER`/
      `REMOVE_PREFIX`/`REMOVE_RETAIN`/`REMOVE_DELETE`, `_is_remove`,
      `_removal_keyboard`, `handle_remove` and `handle_remove_choice` verbatim
      into the new module; `app.py` now imports them from
      `kanakko.commands.remove` (`ButtonPress`/`TextMessage`/`_command_arg`
      stay in `handlers.py`, same shared-spine shape as slices 1-2). Dropped
      `check_removal`/`remove_member` from `handlers.py`'s now-stale `db`
      import — `household_roster` and `InlineKeyboardButton`/`InlineKeyboardMarkup`
      stay, still used by `handle_household` and the refund keyboard.
      `test_webhook.py`'s `REMOVE_PREFIX`-routed callback tests patch
      `app_module.handle_remove`/`handle_remove_choice` directly (routing-level,
      unaffected by which module they live in) — only its `REMOVE_PREFIX` import
      needed retargeting. `test_member_removal.py`'s handler-layer tests
      (`send_message`/`edit_message_text`/`answer_callback_query`) retargeted
      at the new `remove_command` module, same pattern as
      `test_account_command.py`. Verified red-without-fix: reran with the
      `send_message`/`edit_message_text`/`answer_callback_query` patches left on
      `handlers` after the move — 10 failed on `InFailedSqlTransaction` (the real
      send path ran, same failure class slices 1-2 found), not a clean assertion
      mismatch. `handlers.py` is now 1116 lines; `uv run pytest` → 457 passed,
      `ruff check` clean.
- [x] `handlers.py` split, slice 4/6: `kanakko/commands/invite.py`
      (`handle_invite` + `handle_invite_signup`, both `/invite*` commands share
      the same shape). Retarget `tests/test_invite.py` and
      `tests/test_invite_signup.py`. Both handlers, their `_is_invite`/
      `_is_invite_signup` guards, and their `INVITE_*` constants moved verbatim.
      Dropped now-stale `secrets`/`is_admin`/`get_bot_username`/
      `create_household_invite`/`create_signup_invite` imports from
      `handlers.py`. `test_invite.py` retargeted its `handlers.send_message`/
      `get_bot_username` patch and `INVITE_*`/`_is_invite` reads at the new
      `invite_command` module, same pattern as slices 1-3. `test_invite_signup.py`
      needed patches on **both** modules — its end-to-end test also drives
      `handle_start`, which stays in `handlers.py` and sends through that
      module's own `send_message` binding — a single-module patch would leave
      that call live. Verified red-without-fix: patched `handlers.send_message`
      instead of `invite_command.send_message` in `test_invite.py` and reran —
      4 failed on `InFailedSqlTransaction` (real send path, same failure class
      slices 1-3 found), not a clean assertion mismatch. `handlers.py` is now
      994 lines; `uv run pytest` → 457 passed, `ruff check` clean.
- [x] `handlers.py` split, slice 5/6: `kanakko/commands/recurring.py`
      (`handle_recurring` + `_match_category_and_account`). Retarget
      `tests/test_recurring_command.py`.
      Done: moved `RECURRING_COMMAND`/`RECURRING_USAGE`/`RECURRING_BAD_AMOUNT`/
      `RECURRING_BAD_DAY`/`RECURRING_BAD_CATEGORY_ACCOUNT`/
      `RECURRING_NO_HOUSEHOLD`, `_is_recurring`, `_match_category_and_account`
      and `handle_recurring` verbatim into the new module; `app.py` now imports
      them from `kanakko.commands.recurring`. Dropped `create_recurring_rule`/
      `household_accounts`/`EXPENSE_CATEGORIES` from `handlers.py`'s now-stale
      imports (`ALL_CATEGORIES`/`CATEGORY_PREFIX` stay — still used by
      `handle_category`). `tests/test_recurring_command.py` retargeted its
      `handlers.send_message` patch and `handlers.RECURRING_*`/
      `handlers.EXPENSE_CATEGORIES` reads at the new `recurring_command`
      module, same pattern as slices 1-4; `test_webhook.py`'s
      `handle_recurring`-routing test patches `app_module` directly, unaffected
      by the move. Verified red-without-fix: patched `handlers.send_message`
      instead of `recurring_command.send_message` and reran — all 10 tests
      failed with `RuntimeError: TELEGRAM_BOT_TOKEN is not set` (the stub never
      intercepted, the real send path ran), same failure class slices 1-4
      found. `handlers.py` is now 864 lines; `uv run pytest` → 457 passed,
      `ruff check` clean.
- [x] `handlers.py` split, slice 6/6: `kanakko/commands/refund.py`
      (`handle_refund` + `handle_refund_choice`). Retarget
      `tests/test_refund_ux.py` and check `test_webhook.py`'s
      `REFUND_PREFIX`-routed tests the same way slice 3/6 must. After this
      slice, re-measure `handlers.py`'s line count against the 300-line
      guideline — the spine (dispatch/start/text/undo/help/household/confirm
      core) will likely still be ~700+ lines, which may need its own follow-up
      split (e.g. the confirm-card core into its own module) rather than being
      assumed done once the six commands are out.
      Done: moved `REFUND_WORD`/`REFUND_PREFIX`/`REFUND_USAGE`/`REFUND_BAD_AMOUNT`/
      `REFUND_NO_CANDIDATES`/`REFUND_GONE`/`REFUND_OVER_LIMIT`, `_is_refund`,
      `_refund_keyboard`, `handle_refund` and `handle_refund_choice` verbatim
      into the new module; `app.py` now imports them from
      `kanakko.commands.refund`. Dropped `create_refund`/`refund_candidates`
      (db), `date`/`Decimal`, `InlineKeyboardButton`/`InlineKeyboardMarkup` and
      `kanakko.parse.today` from `handlers.py`'s now-stale imports — none had
      another caller left. `tests/test_refund_ux.py` retargeted its
      `handlers.send_message`/`edit_message_text`/`answer_callback_query`
      patches and `handlers.REFUND_*`/`handlers._is_refund` reads at the new
      `refund_command` module, same pattern as slices 1-5;
      `test_webhook.py`'s `REFUND_PREFIX`-routed test patches
      `app_module.handle_refund` directly and imports no `REFUND_PREFIX`, so
      it needed no change. Verified red-without-fix: patched
      `refund_command._stale_send_message` instead of `refund_command.send_message`
      and reran — 5 of 15 tests failed (an `AttributeError` on the stale patch
      target itself, plus a real-write leak once the stub stopped intercepting
      `send_message`), same failure class slices 1-5 found. `handlers.py` is
      now 728 lines; `uv run pytest` → 457 passed, `ruff check` clean. The
      spine is still 728 lines against the 300-line guideline — filed as a
      follow-up split task below rather than assumed done.
- [x] `handlers.py` follow-up split: after all six command slices, the spine
      (dispatch/start/text/undo/help/household/confirm core) is still 728
      lines against the 300-line guideline — the six-slice plan didn't get it
      under the line by itself. Look at the confirm-card core
      (`handle_confirm`/`handle_cancel`/`handle_change_amount_request`/
      `handle_amount_reply`/`handle_account_choice`/`handle_category`) as the
      next seam — it's the largest cohesive block left and shares no state
      with `dispatch`/`handle_start`/`handle_text`/`handle_undo`/`handle_help`/
      `handle_household`, the same shape the six commands already split on.
      Done: `kanakko/confirm_flow.py` — a sibling of `kanakko/commands/`, not a
      member of it, since these handlers answer confirm-card button taps and a
      typed amount reply, not a slash command. Moved the six functions and the
      `CHANGE_AMOUNT_PROMPT`/`CHANGE_AMOUNT_RETRY_PROMPT` constants (only used
      inside them) verbatim; `app.py` now imports them from
      `kanakko.confirm_flow` instead of `kanakko.handlers`. Dropped
      `cancel_pending`/`confirm_pending`/`request_amount_change`/
      `set_pending_account`/`set_pending_amount`/`set_pending_category`,
      `answer_callback_query`/`delete_message`/`edit_message_text`,
      `ALL_CATEGORIES`/`CATEGORY_PREFIX`, `ACCOUNT_PREFIX`/`SKIP_LABEL`/
      `settled_card`, `Transaction` and `parse_amount` from `handlers.py`'s
      now-stale imports — `list_accounts`/`get_or_create_user`/`confirm_card`/
      `send_message` stay, still used by `handle_text`. `tests/test_webhook.py`
      retargeted every `monkeypatch.setattr(handlers, …)` for these six
      handlers at the new `confirm_flow` module (the ones for `handle_text`/
      `handle_undo` stay on `handlers`, unaffected). Verified red-without-fix:
      reverted one retarget back to `handlers` and reran — `AttributeError:
      <module 'kanakko.handlers'> has no attribute 'answer_callback_query'`
      (the symbol doesn't exist there any more, an even sharper signal than
      the "stub never intercepted" failures the six command slices found),
      restored, green again. `handlers.py` is now 491 lines (still over the
      300-line guideline — the spine itself may need a further split later,
      not assumed done here); `kanakko/confirm_flow.py` is 263.
      `uv run pytest` → 457 passed, `ruff check` clean.
- [x] The reconcile nudge (§18) — the adjustment write path (split 1/2). Split
      the same way 014/015 split refunds/recurring: the money-correctness core
      (compute the drift, write a **visible** row, never a silent rewrite)
      lands first; the weekly cron nudge and the bot-side reply that supplies
      `reported_balance` are UX wiring on top of it — a genuinely separate
      task (new per-user "awaiting a reconcile reply" state, the same shape
      017's `awaiting_amount` needed for "Change amount"), not a deferrable
      stub of this one.
      Done: `kanakko.db.reconcile.create_adjustment(conn, user_id, account_id,
      reported_balance, occurred_on, *, source, update_id)`. No schema change —
      an adjustment is an ordinary `transfer` against the household's `external`
      account (011 already has the columns and CHECK); migration `018` only
      widens `transaction_events.action` to admit `'adjustment'`, distinct from
      `'confirm'` so the audit query can tell a system correction from a
      user-typed transfer. Recomputes the account's presented balance with the
      same formula and credit-sign convention `accounts.account_balances` uses
      (§18), then derives the transfer algebraically: `delta_raw = sign * drift`
      (`sign` −1 for `credit`, else +1) makes the transfer amount `abs(delta_raw)`
      and its direction the sign of `delta_raw`, so a `credit` account owing more
      than the ledger thought moves money *out* of the card (the same direction
      an ordinary swipe already moves it) rather than needing a kind-branched
      special case. Zero drift writes nothing and returns `None`. Household-scoped
      (§16, an `EXISTS`-shaped account lookup) and refuses the `external` account
      itself (structural, never something a nudge is sent for). No caller yet,
      same as `create_refund`/`create_recurring_rule` when their schemas landed.
      The named guard verified two ways: (a) reverted migration 018, reran
      `tests/test_reconcile.py` — 3 of 6 failed on `CheckViolation`, restored,
      green; (b) rewrote the write to silently `UPDATE accounts SET
      opening_balance = ...` instead of inserting a transfer + audit row (the
      exact failure mode §18 names) — the same 3 tests failed because the
      ledger row and audit row the assertions look for don't exist, proving a
      balance-only check would have passed on the broken version. `uv run
      pytest` → 463 passed (6 new), `ruff check` clean.
- [x] The reconcile nudge (§18) — the weekly cron send and the bot-side reply
      (split 2/2). Weekly per account (a new schedule, not daily like the other
      jobs): "I think your Bank has ₹42,300 — what does your bank say?", then
      capture the free-text reply and call `db.reconcile.create_adjustment` with
      it. Needs a per-pending-row "awaiting a reconcile figure" state — the same
      shape `pending_transactions.awaiting_amount` (017) gave "Change amount",
      not a new table — and a decision on where the nudge fits among the
      existing evening/noon/monthly/recurring cron sends.
      Done: migration `019` makes `pending_transactions.parsed` nullable and adds
      `awaiting_reconcile_account_id` — a reconcile ask holds no `Transaction` at
      all, so it rides the same table 017 used for "Change amount" rather than a
      new one, with a CHECK (`(parsed IS NOT NULL) <> (awaiting_reconcile_account_id
      IS NOT NULL)`) keeping the two kinds of pending row from ever blurring.
      `kanakko.db.reconcile` gains the cron's read (`accounts_for_reconcile` —
      cross-household like `due_rules_today`, `external` excluded) and the
      awaiting-reply triple (`create_reconcile_ask`/`pending_awaiting_reconcile`/
      `clear_reconcile_ask`), the same shape `db.pending`'s `awaiting_amount`
      functions have. `kanakko.jobs.reconcile` is the send: its own per-account
      fan-out loop (a household can own several accounts, the same reason
      `jobs.recurring` doesn't use `jobs.fan_out`), phrased per kind — "what does
      your bank say" for `spending`/`locked`, "what does your card statement say
      [you owe]" for `credit` — and quoting `db.account_balances`'s own figure,
      read once per household and reused across its accounts rather than a third
      copy of the balance formula (the maintainability finding on `2fd6537`'s
      review). Weekly Sunday 10:00 IST in the crontab — a judgment call, §12/§18
      name no time, the same way recurring's 08:00 was.
      `kanakko.reconcile_flow.handle_reconcile_reply` is the other half:
      `app.py`'s webhook checks `pending_awaiting_reconcile` for every
      `TextMessage` exactly like it already does for `pending_awaiting_amount`
      (checked second — the two "next message is a reply" states can't both
      claim one message), routing here instead of a fresh parse. An unparseable
      reply leaves the ask outstanding so the user can just retry (`set_pending_
      amount`'s contract); a parseable one clears it and hands the figure to
      `create_adjustment` — a match writes nothing and replies "no changes
      needed", a mismatch writes the visible adjustment and names its size.
      Never metered, never capped — no LLM call, the same free-tap treatment an
      amount reply gets. Two guards verified red-without-fix, green-with-fix:
      migration 019's CHECK (dropped it, watched both the "neither set" and
      "both set" inserts stop raising `CheckViolation`) and the webhook routing
      branch (removed it, watched the reply fall through to `handle_text`
      instead of `handle_reconcile_reply`). `uv run pytest` → 473 passed (10
      new), `ruff check` clean.

### After the code

- [x] Add a Phase 10 section to `docs/TESTING.md` — the double-count case (swipe
      then pay the bill), an FD round trip, a partial refund and an attempt to
      over-refund, the daily path still being one tap, and a reconcile that
      leaves a visible row.
      Done: new `## 9. Accounts, transfers, and reconciliation` (18 rows,
      9.1–9.18), plus a `Summary` table row and its two money-path callouts
      (9.6 double-count, 9.13 over-refund) added to the closing "stops a
      release" line, matching how 1.6/2.4/3.6/8.3/8.4 are already called out.
      Manual-test doc only — no code, no `pytest` surface to run.
- [ ] `[human]` Re-run the manual QA that Phase 10 touches, and set opening
      balances on your own accounts before demoing.

---

## Phase 11 — Opening balances, and what the Phase 10 review found

Raised 2026-08-16 from using the shipped bot and re-reading Phase 10 against
§18. Nothing here is a new idea: every item is either §18 asking for something
the implementation narrowed, or a defect found after the merge.

### First — the dashboard fails silently on every write

- [x] Surface a failed mutation instead of swallowing it. Every write in
      `webapp/shell.py` ends `\.then(r => { if (r.ok) load(); })` — edit, delete,
      refund, category, recurring pause, recurring delete, **six copies, none with
      an `else` and none with a `.catch`**. So a 4xx, a 5xx, a dropped connection
      or an expired `initData` (the mutating routes enforce a 24h `auth_date`
      window) produces *nothing at all*: the field keeps showing the value the
      user typed, the server keeps the old one, and the next `load()` — which
      fires on every re-activation — silently reverts it. The user's conclusion is
      that the app randomly forgets edits, on a money screen.
      Replace all six with **one** helper that always calls `load()` and shows a
      toast when the response is not ok or the fetch throws. Always reloading is
      the load-bearing half: it discards the optimistic local value and re-renders
      the server's truth, so a failed edit visibly snaps back rather than lying.
      One helper also deletes six copies of the same fetch block (CLAUDE.md, one
      definition per thing). Guard: stub a route to 500 and assert the value on
      screen returns to the stored one *and* the toast appears — a test that only
      checks the toast passes on a version that leaves the wrong number showing.
      Done: added `mutate(url, body)` in `webapp/shell.py`, replacing all six
      inlined fetches; it always `.finally(load)`s and calls `showToast()` (a
      fixed-position `#toast` div) on `!r.ok` or a thrown/network error. No
      DOM/browser test harness exists in this repo (the existing shell.py tests
      are all structural, per `test_txn_row_places_note_and_delete`'s own note),
      so the guard (`test_a_failed_mutation_is_surfaced_not_swallowed`) pins the
      mechanism — `load()` unconditional in `.finally`, the toast strictly on
      failure, all six call sites routed through `mutate(` — rather than driving
      a real browser; verified it reddens against the reverted single-copy bug.

### The one that can corrupt a number — read before starting

`db/accounts.py::set_account_opening_balance` finds the account to update **by
kind**:

```sql
WHERE household_id = (…) AND kind = %s AND deleted_at IS NULL
```

That was safe while only `credit` and `locked` came through it — one account per
kind. **`Bank` and `Cash` are both `kind = 'spending'`**, so the moment this
command accepts them, `/account cash 2000` finds *Bank* and overwrites the bank's
opening balance with ₹2,000. No error, no warning, and the reported figure looks
perfectly plausible. Once both exist, `fetchone()` picks between two matching rows
arbitrarily.

The first task below fixes it. **Do not extend the command to spending accounts
without keying the lookup on name** — and prove it the way CLAUDE.md requires:
set Bank to 52,000, then set Cash to 2,000, then assert Bank is *still* 52,000.
Cause the corruption first and watch the check go red, or it is a claim rather
than a guarantee.

### Opening balances

- [x] Let `/account` set a **spending** account's opening balance —
      `/account bank 52000`, `/account cash 2000` — creating Cash on first use.
      §18 says the onboarding ask covers "an opening balance for each"; what
      shipped covers only `credit` and `locked`, so a household's bank sits at ₹0
      with no way to correct it but a reconcile.
      **Key the lookup on name, not kind** — see the warning above; this is the
      task that makes the collision reachable, so it is the task that must close
      it. `credit` and `locked` keep their current wording ("what you owe" /
      "what's already in it") and their sign handling; a spending account is asked
      plainly what is in it. Guard: Bank 52,000 then Cash 2,000 leaves Bank at
      52,000, and it reddens if the lookup still keys on kind.
      Done: `set_account_opening_balance` (`kanakko/db/accounts.py`) gains a
      `name` param and now matches `WHERE kind = %s AND lower(name) = lower(%s)`
      — kind *and* name together, not kind alone (name-only would trade the
      Bank/Cash collision for a new one: a spending account named "card" would
      match the fixed `credit` account "Card", since `accounts.name` has no
      uniqueness constraint). `credit`/`locked` keep passing no `name` and get
      their fixed onboarding name as before, so every existing call site and test
      is unchanged. `handle_account` (`kanakko/commands/account.py`): a first
      word that isn't `credit`/`locked` and doesn't match a live `locked`
      account, followed by a word that parses as an amount, now sets that
      spending account (creating it on first use) instead of refusing with
      `ACCOUNT_BAD_KIND`, which is now unreachable and removed along with its
      test. `ACCOUNT_USAGE` reworded to mention spending accounts. Guard
      (`test_set_account_opening_balance_keys_on_name_not_just_kind` in
      `tests/test_accounts.py`) verified red without the name filter — reverted
      the `WHERE` clause to kind-only, watched it fail, restored it. `uv run
      pytest` → 479 passed.
- [x] Point at it once from the welcome message — one line, e.g. *"Want your
      balance to be right? Tell me what you have: `/account bank 52000`."*
      Deliberately **not** an onboarding questionnaire: a multi-step "which
      accounts, how much in each" conversation is the state machine §5 rejected as
      the largest source of bugs in a Telegram bot, and it blocks the first thing
      a new user came to do. Skipping it costs nothing — a later reconcile lands
      the same numbers (verified: an adjustment is written as a `transfer`
      (`db/reconcile.py:229`), which is excluded from both totals, and an opening
      balance never enters those reports either). Add the same line to `/help`.
      Done: added the line verbatim to `WELCOME` (after the credit/locked
      paragraph) and a matching `/account bank 52000 — set what a spending
      account like Bank or Cash holds` line to `HELP_TEXT`, alongside the
      existing `/account credit`/`/account locked` lines.
      `test_setting_a_spending_account_balance_is_pointed_at_from_welcome_and_help`
      (`tests/test_help.py`) verified red without the fix (reverted the
      `handlers.py` change, watched it fail on the `WELCOME` assertion, restored
      it). `uv run pytest` → 480 passed.

### Defects found after the Phase 10 merge

- [x] Gate the dashboard's per-row account dropdown the way the confirm card is
      gated. `webapp/recent.py::_row_editor` renders `_account_select` for every
      non-transfer row with no count check, so a household with one account gets a
      `<select>` holding a single option — a dead control that reads as a failed
      load, which is exactly how it was reported. `confirm.py:126` already has the
      rule (`show_accounts = bool(accounts) and len(accounts) > 1`); §18's
      "accounts become visible only when a second one exists" applies to both
      surfaces, and right now only one obeys it.
      Done: `_edit_panel` (`kanakko/webapp/recent.py`) now only calls
      `_account_select` when `type_ != "transfer" and len(accounts) > 1` — the
      same test `confirm.py:126` already applies. Guard
      `test_recent_list_edit_panel_hides_account_select_for_a_single_account`
      (`tests/test_webapp.py`) verified red without the `len(accounts) > 1`
      check (reverted it, watched the assertion fail on a live
      `class="edit-account"` in the output, restored it). `uv run pytest` →
      481 passed.
- [x] Show account balances in the dashboard. `account_balances` has exactly one
      consumer — the weekly reconcile job — so "how much do I have", the headline
      number of the whole accounts feature, is answered once a week in a Telegram
      message and nowhere a user can look. **This is the one item in this phase
      that is a new surface rather than a fix**; drop it if the phase needs to be
      shorter. `credit` reads as what is owed (§18's sign convention, already in
      the query); `external` stays hidden as everywhere else.
      Done: `webapp/render.py::account_balances_section` — one `.stat` row per
      live, non-`external` account (`db.account_balances`'s own list, its
      `credit` sign convention already baked into `balance`), reusing the
      `.stat`/`.substats` markup and CSS the period panels already define, so no
      new styling was needed. Rendered in `dashboard_html` right after the period
      panels and before `recurring_list` — the flow figure, then the stock
      figure, then automation, then detail. `mini_app_data` (`webapp/routes.py`)
      queries `account_balances(conn, user_id)` alongside the other reads and
      threads it through as the new `balances` param. Guard
      (`test_account_balances_section_renders_a_row_per_account_and_hides_external`)
      verified red without the `external` filter — briefly changed it to
      `list(balances)`, watched "External" leak into the output, restored it. A
      route-level test (`test_dashboard_route_shows_account_balances`) drives the
      real route against Postgres: the default account's income and a fresh
      `credit` account's opening balance both show, positively signed for the
      card. `uv run pytest` → 484 passed.

### The row editor, reviewed against the live UI (2026-08-16)

From four screenshots of a real row plus a read of `webapp/recent.py` and the
`.txn` grid in `webapp/shell.py`. Credit where due first: every control already
carries a real `aria-label` ("Delete ₹500.00 on 16 Aug"), so screen readers are
fine — it is **sighted** users who get five unlabelled grey boxes.

- [x] Rebuild the row: actions out of the collapsed row, category in, fields
      labelled and reordered. One task, not five — they are the same markup, and
      doing them separately means restyling it four times.
      **Why the collapsed row changes:** `.del` is grid-row 1, `.edit-toggle` row
      2, `.refund-toggle` row 3, all column 2 at 44px, so **every expense row is
      at least 132px tall** for content needing 44–64px. A Mini App viewport shows
      about four transactions where it could show ten. They are also three
      near-identical grey glyphs, one of which deletes money, in the lane the
      thumb reaches for; `↩` reads as "reply", not "refund".
      Collapsed shows data only — `16 Aug · Food · −₹500.00`, note beneath —
      and the whole row expands on tap. The panel ends with **labelled** `Delete`
      and `Refund` buttons, words not glyphs, below a rule that separates them
      from the corrective fields: those two create and destroy, the fields above
      only correct. Delete becoming two deliberate taps is the point, not a cost.
      **Category moves into the panel** and the header becomes plain text. Its
      `<select>` chevron currently sits beside a row that expands, so it reads as
      "expand this row" — two chevron-ish affordances for one meaning. The
      `/app/category` route may stay as it is; this is about where the control
      lives, not which endpoint it calls.
      **Labels left, field right** (two columns, label ~90px). This is what kills
      the worst state in the screenshots: with both panels open there are two
      identical `500.00` boxes, one silently overwriting the amount and one
      issuing a refund. Do **not** pair date and amount side by side — a date
      input with its picker is ~140–150px and will wrap unpredictably at 360px.
      **Order by consequence:** Amount, Category, Date, Account, Note. Category
      second because §5 says outright it is the most-often-wrong field; note last
      because it is cosmetic.
      Also here, since it is the same markup: label the amount `Amount (₹)` — the
      field shows a bare `500.00` while the header shows `−₹500.00` — and put a
      small helper beside the date rendering it as `16 Aug 2026`. `<input
      type="date">` formats to the *device* locale and cannot be styled, so an
      Indian user sees `08/16/2026` under a header reading `16 Aug`; keep the
      native input (§7, §13 are right about native-first) and disambiguate beside
      it. **Preserve the one-account dropdown gate** from the task above — it
      lands first and this rebuild must not undo it.
      Guard: a row with a note and one with none both render at the same height,
      and that height is well under 132px.
      Done: `kanakko/webapp/recent.py` — the collapsed row is one `<button
      class="txn-head">` showing date, category (plain text now — a `<select>`
      never lived in the always-visible part again), amount and the note
      beneath; tapping it toggles the sibling `.txn-panel` (`aria-expanded`
      flipped in `shell.py`'s click handler, replacing the old `.edit-toggle`
      glyph). `_txn_panel` renders the five fields via a new `_field(label,
      control)` helper — Amount (labelled "Amount (₹)"), Category (the existing
      `_category_select`, only for a non-transfer row), Date (with a
      `.date-human` "16 Aug 2026" span beside the native input), Account (only
      `type_ != "transfer" and len(accounts) > 1` — the gate from the task above,
      unchanged), Note — then a `<hr class="txn-rule">` and labelled
      Delete/Refund buttons (`>Delete<`, `>Refund<`, no glyphs); Refund still
      reveals `_refund_panel` (its full-amount default is the next task).
      `shell.py`'s CSS dropped the three `grid-row`-pinned 44px lanes entirely
      (`.del`/`.edit-toggle`/`.refund-toggle` no longer exist as always-visible
      grid children) in favour of one `.txn-head { min-height: 44px }` button —
      the mechanism that bounded every row to 132px regardless of content is
      gone, not hidden. Guards, all verified red-without-fix, green-with-fix:
      `test_recent_list_renders_a_labelled_editor_panel_reached_by_tapping_the_row`
      (field order + labels + the human date), `test_recent_list_header_shows_category_as_text_not_a_dropdown`,
      `test_recent_list_delete_and_refund_are_labelled_and_only_inside_the_panel`
      (reddens if either button reappears in the collapsed head),
      `test_recent_list_head_button_carries_aria_expanded`, and
      `test_collapsed_row_height_no_longer_depends_on_which_actions_it_has`
      (replaces `test_txn_row_places_note_and_delete` — pins the absence of any
      `grid-row` rule and that neither a note-carrying nor a note-less collapsed
      row exposes `.del`/`.refund-toggle`, the same mechanism-pinning shape the
      test it replaces used, per its own note that a rendered pixel height needs
      eyes on a phone, not a headless assertion). `uv run pytest` → 487 passed,
      `ruff check` clean.
- [x] Undo a delete, rather than confirm it. `.del` fires `POST /app/delete`
      immediately — no confirmation, and the ✕ glyph at a card's top-right is the
      universal *dismiss* symbol on a card that also expands, so "close" is a
      reasonable misreading of a control that removes a transaction. §6 already
      soft-deletes, so `deleted_at` is sitting there and un-deleting is one route
      setting it back to NULL. A toast reading `Deleted ₹500.00 · Undo` beats a
      confirmation dialog: it does not tax the case where the user meant it.
      The undo must write its own audit row (§17) — restoring a transaction is a
      money mutation, and migration 013's action set will need a value for it.
      Done: migration 020 widens `transaction_events_action_check` with
      `'restore'`. `kanakko/db/edits.py` gets `restore_transaction` — the one
      place in the codebase that correctly reads `transactions` directly rather
      than the `active_transactions` view, since a soft-deleted row is exactly
      what the view hides; scoped to `user_id`/`household_id` like its siblings,
      writes a `restore` audit row (`after` only, mirroring delete's `before`
      only). `POST /app/restore` in `routes.py` exposes it with the same 24h
      `max_age` and 404-on-no-match shape as `/app/delete`. `_txn_panel`'s
      `.del` button now also carries `data-amount`. `shell.py`: `mutate()` takes
      an optional `onSuccess` callback; the delete click handler passes one that
      calls the new `showUndoToast(id, amount)`, which repaints `#toast` (now
      empty by default, populated by JS either way) as "Deleted ₹500.00 ·
      Undo" for 5s: `.toast-undo`, on its own `toast` click listener (the
      button lives outside `#app`, so the `app` listener never sees it), calls
      `mutate('/app/restore', ...)`. `uv run pytest` → 497 passed, `ruff check`
      clean. Guards, all verified red-without-fix: the audit-row tests
      (constraint value removed from the migration), `restore_of_a_live_row`
      (dropped `deleted_at IS NOT NULL`), and the shell.py wiring test (delete
      call site reverted to skip the toast).
- [x] Stop the refund field defaulting to the full amount, and show what is
      actually left. `_refund_panel` sets `value="{amount}"` under a full-width
      primary button — the only prominent button on the card — so opening it by
      accident puts a complete refund one tap away. Migration 014's trigger stops
      a refund *exceeding* the original; it does nothing about an unintended
      *full* one, which is the likelier mistake. Leave the input empty with the
      ceiling as placeholder, and make that ceiling **what remains**, not the
      original — otherwise the field invites an amount the trigger will reject.
      That means the row data has to carry refunded-so-far, so this is a real
      change to `recent_transactions`, not a markup tweak.
      Done: `db.recent_transactions` (`kanakko/db/reports.py`) gains a 10th
      column, `refunded_so_far` — a correlated subquery summing live refunds
      against each row, the same "amount minus its live refunds" aggregate
      migration 014's trigger enforces and `refund_candidates` already computes
      (0 for anything that isn't a refunded `expense`). `recent_list`/`_txn_panel`
      thread it through as `remaining = amount - refunded_so_far`; `_refund_panel`
      drops the `value="{amount}"` pre-fill for an empty input with `placeholder`
      *and* `max` set to `remaining` — the native HTML5 ceiling, not just a hint.
      Two guards verified red-without-fix, green-with-fix: the placeholder/`max`
      pointing at `remaining` instead of the original amount
      (`tests/test_webapp.py`), and `recent_transactions` itself reporting the
      partial-refund sum rather than a stale `account_id` in that column
      (`tests/test_db.py::test_recent_transactions_reports_what_remains_refundable`).
      `uv run pytest` → 500 passed, `ruff check` clean.
- [x] Render a refund row as a refund. Found 2026-08-16 in a live screenshot: a
      ₹501 refund of a `Health` expense renders as **"Uncategorised · +₹501.00" in
      the income green**, with nothing saying what it refunds. **The data is
      correct** — `refund_of_txn_id` is stored (migration 014) and
      `refunds.create_refund` copies the original's category onto the refund row,
      which is exactly what lets `month_summary` net it with a plain
      `GROUP BY category`. Every fault here is in the rendering. Runs after the two
      tasks above: the rebuild owns the markup, and the refund-default task is what
      puts refunded-so-far into the row data.
      1. **"Uncategorised" is false** — that row's category *is* `Health`.
         `_category_select` builds its options from
         `CATEGORIES_BY_TYPE.get(type_, ())`, which has only `expense` and
         `income` keys, so a `refund` row gets zero options and falls through to
         the disabled placeholder. Render the category as **text, not a select**,
         the way a `transfer` already renders its two account names: a refund's
         category is inherited and must stay in sync with its original or the
         netting silently breaks, so it must not be editable at all. `recent_list`
         branches on `type_ == "transfer"`; `refund` needs the same branch, and
         its absence is why this got through.
      2. **The `+` and the income tint are wrong.** `sign = "−" if type_ ==
         "expense" else "+"` and `amt_cls` follow the same rule, so a refund wears
         the one accent reserved for income — the single thing §18 says a refund
         must never look like. The reports are right and the screen contradicts
         them. Render it neutrally, as a transfer already is.
      3. **Say what it refunds, in words.** `recent_transactions` does not select
         `refund_of_txn_id`; add a `LEFT JOIN` back to `transactions` on it — the
         query already left-joins twice for account names, so it is the same shape
         — and show `Refund of "Spent 500 medicine cash" · 16 Aug` in the note
         lane. **No reference numbers**: `txn_id` is internal identity, "Refund of
         #1284" says nothing about what was refunded, and a personal finance app
         should not make people learn one. The note, date and amount already
         identify a row to a human.
      4. **Close the loop on the original too.** The refunded expense still reads
         a bare `−₹501.00`, so a user has to spot two rows and do the arithmetic.
         Show `· refunded` when it is whole and `· ₹200.00 refunded` when partial,
         off the refunded-so-far the previous task already adds.
      Deliberately **not** nesting a refund visually under its original: the list
      is flat, ordered by entry time and capped at ten, so the original is often
      outside the window — indenting would mean fetching rows past the limit and
      the ordering would stop meaning anything.
      Guard: a refund of a categorised expense renders that category, carries
      neither `+` nor the income class, and names its original — and the check
      reddens if `CATEGORIES_BY_TYPE` regains a `refund` key and papers over it.
      Done: `recent_transactions` (`kanakko/db/reports.py`) gains columns 11/12,
      `refund_of_note`/`refund_of_occurred_on`, via a `LEFT JOIN active_transactions
      orig` on `refund_of_txn_id` — the view, not base `transactions`, so the join
      itself doesn't reopen the §6 bypass `test_read_paths.py` guards against; a
      soft-deleted original just falls back to "Refund of an expense" instead of
      surfacing deleted-row text. `recent_list` (`kanakko/webapp/recent.py`)
      branches `type_ == "refund"` in three places: `_category_known` checks its
      category against `expense`'s set (the one `CATEGORIES_BY_TYPE` has no
      `refund` key for), sign/tint go neutral like a `transfer`'s, and the note
      lane names what it refunds instead of showing its own (always-empty) note.
      `_txn_panel`'s category field renders as text, not `_category_select`, for
      the same "must not be editable" reason a `transfer` skips it entirely. The
      refunded expense gets a `· refunded` / `· ₹200.00 refunded` suffix on its
      own amount span. Six guards verified red-without-fix, green-with-fix (one
      per branch above, plus the DB join itself) by reverting each in turn.
      `uv run pytest` → 507 passed, `ruff check` clean.

### The spec no longer describes the code

- [ ] Reconcile `docs/DECISIONS.md` §5 with the editor that shipped. §5 is titled
      "No field editor" and says "Wrong amount or date → Cancel and retype", while
      `POST /app/edit` now changes amount, date, note, category and account on any
      row. §5's actual rejection was a **chat-side** multi-step editor, and §13
      always said the transaction list existed for "correcting older entries" — so
      the dashboard editor extends §5's own point 4 rather than contradicting it.
      Say that in §5, in place of the line that is now false. `docs/TESTING.md:72`
      repeats the stale claim verbatim and changes with it. Per CLAUDE.md the spec
      is never edited to match the code — this is the other case, a decision that
      was made and never written down, and leaving it is the drift CLAUDE.md calls
      the most expensive damage in this codebase.
      **Blocked, 2026-08-16:** this task's own wording argues for an exception to
      "never edit `docs/DECISIONS.md`", but neither `CLAUDE.md` nor this loop's
      own final rules ("Never edit `docs/DECISIONS.md`. If the spec is wrong or
      self-contradictory, add a task saying so.") carve out that exception — an
      implementer iteration isn't the one positioned to decide the exception
      applies here, since that decision is exactly what's supposed to make it
      into the spec's own changelog/decision record, not a code commit's
      say-so. Leaving unticked: a human should either edit §5 directly, or
      explicitly amend the no-edit rule to name this exception before an
      iteration acts on it.

### The manual test plan is missing three shipped features

- [x] Add manual test rows for **recurring rules** — `/recurring` creation, the
      cron card's Confirm / Change amount / Skip, and dashboard pause and delete.
      Migrations 015-017 shipped an entire feature with not one row in
      `docs/TESTING.md`, and it is the feature most worth testing by hand: it
      writes money on a schedule, unattended, and §18 chose the ask-first card
      precisely because a silently wrong recurring row is worse than a missing one.
      Added `docs/TESTING.md` §10 (17 rows: creation validation incl. a `locked`
      account as a valid target, the cron card's exact button set, Confirm/Change
      amount/Skip, dashboard pause/resume/delete, and 10.17 flagging a real gap
      found while researching — no guard in `kanakko/jobs/recurring.py` stops a
      second card going out for a rule whose card from the same month was never
      resolved). Every command reply, button label, and route in the new rows was
      read from source (`kanakko/commands/recurring.py`, `kanakko/confirm.py`,
      `kanakko/jobs/recurring.py`, `kanakko/webapp/recurring.py`,
      `kanakko/webapp/render.py`, `kanakko/webapp/shell.py`), not guessed.
- [x] Add manual test rows for **dashboard editing and opening balances** —
      editing an amount, a date and an account on an existing row (and that each
      leaves an audit row, migration 013's `edit` action); setting a `credit`
      account's balance and confirming it is asked as *what you owe*; and setting a
      spending account's balance once the first task above lands. Include the
      one-account case on **both** surfaces — no Account line on the confirm card,
      and no dead dropdown in the dashboard.
      Also add a row for the risk found on 2026-08-16 and **not** reproduced: with
      only "Bank" in the account list, "paid the credit card bill 2000" gives the
      model no card to name, and it may mint a **`locked`** account called "Credit
      card" for something that is a card. Check what actually happens and record
      it; if it does misfile, that is a finding for `REVIEWS.md`, not a silent fix
      here.
      This task runs **after** the row rebuild, so write the rows against the new
      editor, not the old one: a collapsed row showing data only, `Delete` and
      `Refund` as labelled buttons inside the panel, the delete-undo toast, and —
      the one no automated check can cover — **turn the network off mid-edit and
      confirm the value snaps back with a toast** rather than sitting on screen
      looking saved.
      Added `docs/TESTING.md` §3c (9 rows: expand-to-edit, amount/date/account
      fields, the panel re-collapsing after every edit — `shell.py`'s `mutate()`
      always reloads, so a second edit needs re-expanding, which 3c.2 calls out
      by name since it reads like a bug — Delete/Refund as labelled buttons, the
      undo toast and its auto-dismiss, and the network-off case: offline, the
      post-edit reload fails too, so the whole app area says "Could not load
      dashboard" rather than just the one field reverting), two rows in §5a
      (5a.19-20, same query shape as 5a.7, confirming the `edit` audit action
      for amount/date/account), and four in §9 (9.19-22: no Account field with
      one account, `/account credit`'s exact "you owe" reply, `/account
      <name>`'s "you have" reply for a spending account, and 9.20 for the
      credit-card-bill risk). Read `parse.py` for 9.20 rather than guessing:
      `new_locked_account`/`transfer`/the account-vocabulary prompt text are all
      gated on `len(accounts) > 1` (`parse_schema`, `build_request`), so a
      single-account household's prompt carries none of them — the specific
      misfile named in this task (minting a `locked` "Credit card" account)
      cannot happen through that path. The row asks the tester to record what
      the model does instead, since that's still unverified without a live call.
      9.19-20 must run before 9.2, which permanently ends the single-account
      state — noted inline since renumbering 9.2 onward would have touched
      `REVIEWS.md` and `docs/DECISIONS.md`'s existing row references.
      `uv run pytest` → 508 passed (docs-only change).

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
