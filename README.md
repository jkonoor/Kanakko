# Kanakko

A personal finance tracker you talk to. Send a Telegram message like
`Spent ₹500 on groceries`, confirm the parsed result with one tap, and it's
recorded. A Telegram Mini App shows the totals, balance, category breakdown,
and weekly/monthly summaries.

The problem it solves: tracking income and expenses fails not because the
tooling is missing but because the tooling demands discipline. Kanakko moves
entry into a chat you already have open and reduces each transaction to one
sentence and one tap.

## Status

Design complete, implementation not started. See
[docs/DECISIONS.md](docs/DECISIONS.md) for what was decided and why, and
[docs/PLAN.md](docs/PLAN.md) for the build order.

## Shape

| Piece | Choice |
|---|---|
| Entry | Telegram bot, natural language, one LLM call per message |
| LLM | OpenRouter (`claude-opus-5` default), JSON-schema structured output |
| Backend | Python · FastAPI · `python-telegram-bot` · PostgreSQL |
| Data access | Plain SQL via `psycopg`, numbered `.sql` migrations — no ORM |
| Dashboard | Telegram Mini App, auth via `initData` HMAC — no login |
| Scheduling | Cron sidecar container (noon nudge, 9pm summary, 1st-of-month report) |
| Hosting | Innogenio `doc-panel` Dokploy instance |

Deliberately absent: ORM, Celery, Redis, charting library, conversation state
machine, login system. Each is noted in `docs/DECISIONS.md` with the condition
that would justify adding it.

## Scope

Single user (the author) for now. The schema carries `user_id` on every table
and the Mini App already authenticates a Telegram user ID, so multi-tenancy is
a small change rather than a rewrite — but no auth, signup, or billing code
exists and none should be added until a second user actually exists.

## Repository layout

```
docs/DECISIONS.md    what was decided, why, and what was deliberately deferred
docs/PLAN.md         build order, phase by phase
docs/DEPLOYMENT.md   Dokploy specifics, API sequence, known gotchas
```
