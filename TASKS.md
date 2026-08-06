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
- [ ] Write `reminder_log` rows from every job; make the noon suppression read it
- [ ] Add the crontab for the `cron` service with all three entries
- [ ] Add a check that a transaction at 23:50 IST on a month's last day lands in that month's report

## Phase 4 — Mini App dashboard

- [ ] Add `initData` HMAC validation — `HMAC-SHA256(bot_token, "WebAppData")` as the secret key, per `docs/DECISIONS.md` §13
- [ ] Add a check that a forged or tampered `initData` payload is rejected
- [ ] Add the dashboard route rendering totals, balance, and current-month figures
- [ ] Add the category breakdown as a sorted list with CSS percentage bars — no charting library
- [ ] Add the weekly and monthly summary sections
- [ ] Add the recent-transactions list with per-row soft delete
- [ ] Add per-row category change from the dashboard
- [ ] Make the dashboard render correctly in both light and dark themes
- [ ] `[human]` Register the Mini App menu button with BotFather

## Phase 5 — Backups and hardening

- [ ] `[human]` Create the Dokploy backup for `kanakko-db` (`backupType: "database"` — the managed resource, so no `metadata` field needed) — see `docs/DEPLOYMENT.md`
- [ ] `[human]` Trigger a manual backup, restore it into a scratch database, verify the row count
- [ ] `[human]` Confirm every secret lives in Dokploy env and none is in the repo

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
