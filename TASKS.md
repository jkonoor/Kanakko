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
- [ ] Write `migrations/001_init.sql` — `users`, `transactions`, `pending_transactions`, `reminder_log`, and the `active_transactions` view, per the data model in `docs/PLAN.md`
- [ ] Add a migration runner that applies `migrations/*.sql` in order and records which have run
- [ ] Add `docker-compose.yml` with `web`, `db` (postgres), and `cron` services; `cron` uses the same image with a cron command and `TZ=Asia/Kolkata`
- [ ] Add `.env.example` with every required key and empty values
- [ ] `[human]` Create the Dokploy project, deploy, attach domain, verify `/healthz` over HTTPS — see `docs/DEPLOYMENT.md`

## Phase 1 — The core loop

- [ ] Add `kanakko/categories.py` — the expense and income lists from `docs/DECISIONS.md` §11, plus helpers that emit the JSON-schema enum and the Telegram keyboard from the same constant
- [ ] Add `kanakko/money.py` — Decimal parsing/formatting for ₹ amounts, rejecting float anywhere
- [ ] Add a check covering parse → store → sum-by-category using `Decimal`
- [ ] Add `kanakko/parse.py` — OpenRouter call with `response_format` JSON schema and `require_parameters: true`, per `docs/DECISIONS.md` §2
- [ ] Make `parse.py` inject the current `Asia/Kolkata` date into every prompt so relative dates resolve
- [ ] Add Pydantic validation of the parse result plus exactly one retry on schema failure
- [ ] Make `category` nullable in the schema and `amount` non-nullable, per `docs/DECISIONS.md` §3
- [ ] Add the Telegram webhook endpoint and update dispatch
- [ ] Render the confirm card: amount, type, category, date, note, with Confirm and Cancel buttons
- [ ] Handle Confirm — write to `transactions`, clear the pending row
- [ ] Handle Cancel — discard the pending row, acknowledge
- [ ] Reject messages with no parseable amount with a rephrase prompt, storing nothing
- [ ] Show category buttons instead of the confirm card when `category` came back null

## Phase 2 — Corrections

- [ ] Category buttons on the confirm card, generated from `categories.py`
- [ ] Handle a category button press — update the pending row, re-render the card
- [ ] Add `/undo` — soft-delete the most recent confirmed transaction, confirm what was removed
- [ ] Verify every read path goes through `active_transactions`, not `transactions`

## Phase 3 — Scheduled jobs

- [ ] Add `kanakko/jobs/evening.py` — 21:00 daily, unconditional, day's total and entry count
- [ ] Add `kanakko/jobs/noon.py` — 12:00 daily, suppressed if anything was logged since the last evening summary
- [ ] Add `kanakko/jobs/monthly.py` — 09:00 on the 1st, previous month's income, expenses, balance, top categories
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

- [ ] `[human]` Create the Dokploy backup for the compose Postgres, **including the `metadata` field** — see `docs/DEPLOYMENT.md`
- [ ] `[human]` Trigger a manual backup, restore it into a scratch database, verify the row count
- [ ] `[human]` Confirm every secret lives in Dokploy env and none is in the repo

---

QA findings are in [`REVIEWS.md`](REVIEWS.md), not here.
