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
- [ ] `[human]` Register the Mini App menu button with BotFather

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

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
