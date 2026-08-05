# Build plan

Ordered so the riskiest unknown is proven first and every phase ends with
something demonstrably working. Rationale for each choice is in
[DECISIONS.md](DECISIONS.md); deployment mechanics are in
[DEPLOYMENT.md](DEPLOYMENT.md).

## Data model

Four tables. `user_id` on every one from day one (see DECISIONS §1).

```sql
users            -- one row for now; exists so user_id is a real FK later
  user_id, telegram_user_id, timezone, created_at

transactions
  txn_id, user_id, amount NUMERIC(12,2), type, category,
  note, occurred_on DATE, created_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ NULL

pending_transactions  -- parsed, awaiting confirm; short-lived
  pending_id, user_id, telegram_message_id, parsed JSONB, created_at

reminder_log     -- what was sent when; drives noon suppression
  reminder_id, user_id, kind, sent_at
```

`occurred_on` (a DATE, when the money moved) is deliberately separate from
`created_at` (when it was logged) — they differ whenever "yesterday" is used,
and reports must bucket on the former.

All reads go through `active_transactions` (DECISIONS §6).

---

## Phase 0 — Walking skeleton, deployed

Prove the deployment path before there is anything to deploy.

- FastAPI app with a single `/healthz` endpoint
- `docker-compose.yml`: `web`, `db` (Postgres), `cron` (same image, cron command)
- Migration runner + `001_init.sql`
- Deploy to `doc-panel`, domain + TLS, verify `/healthz` returns 200 over HTTPS

**Done when:** `curl https://<domain>/healthz` returns 200 and the Postgres
container holds the migrated schema.

Doing this first means the Dokploy gotchas (see DEPLOYMENT.md) surface against
a trivial app rather than a half-finished one.

---

## Phase 1 — The core loop

The product is this phase; everything else is around it.

- Telegram webhook registered, receiving updates
- OpenRouter client: prompt + JSON schema, **current `Asia/Kolkata` date
  injected every call**, Pydantic validation, one retry on schema failure
- Confirm card: parsed fields + `Confirm` / `Cancel` buttons
- `Confirm` writes to `transactions`; `Cancel` discards
- Reject messages with no parseable amount, asking for a rephrase

**Done when:** `Spent ₹500 on groceries` and `Received ₹30,000 salary` both
round-trip from message to stored row, and `Spent money yesterday` is rejected
cleanly rather than stored with a guessed amount.

**Test to leave behind:** an assert-based check over parse → store →
sum-by-category using `Decimal`, covering the money path (DECISIONS §9).

---

## Phase 2 — Corrections

- Category buttons on the confirm card, driven by the shared category constant
- `/undo` — soft-deletes the most recent confirmed transaction
- `active_transactions` view in use by every read path

**Done when:** a miscategorised entry is fixed in one tap, and `/undo` removes
the last entry from every total without deleting the row.

---

## Phase 3 — Scheduled jobs

Three entries in the cron sidecar, all in `Asia/Kolkata`.

| Job | Cron | Notes |
|---|---|---|
| Noon nudge | `0 12 * * *` | Skip if any transaction logged since the last evening summary |
| Evening summary | `0 21 * * *` | Always send; include day's total and count |
| Monthly report | `0 9 1 * *` | Previous calendar month, bucketed `AT TIME ZONE 'Asia/Kolkata'` |

Each job writes to `reminder_log` — that's what the noon suppression reads.

**Done when:** all three fire at the right local wall-clock times, and the noon
nudge stays silent on a day where something was already logged.

**Watch for:** month boundaries. Verify a transaction logged at 23:50 IST on
the 31st appears in that month's report, not the next one.

---

## Phase 4 — Mini App dashboard

- `initData` HMAC validation (~15 lines, stdlib — see DECISIONS §13)
- Server-rendered page: totals, balance, category bars, weekly + monthly summary
- Recent transactions list with per-row delete (soft) and category change
- Menu button in the bot to open it

**Done when:** the Mini App opens from the chat, shows correct figures, and a
forged `initData` payload is rejected.

**No charting library.** Sorted list with CSS percentage bars.

---

## Phase 5 — Backups and hardening

- Dokploy `backup.create` against the compose Postgres, scheduled, to S3 —
  **including the `metadata` field**, without which it silently fails
  (see DEPLOYMENT.md)
- **Restore-test it.** A backup that has never been restored is not a backup
- Confirm secrets (bot token, OpenRouter key) are in Dokploy env, not the repo

**Done when:** a scheduled backup has run *and* been restored into a scratch
database with the row count verified.

---

## Not in scope

Listed so they aren't drifted into: recurring/scheduled transactions, budgets
and limits, multi-currency, receipt/photo parsing, bank or SMS import, shared
or family accounts, data export. Each is a reasonable future feature and none
is needed to answer "how much did I earn, spend, and save."
