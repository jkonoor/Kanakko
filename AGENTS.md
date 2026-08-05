# Working on Kanakko

How to build, run, and test this project, and the conventions any change must
follow. Read this before writing code.

The **specification is [`docs/DECISIONS.md`](docs/DECISIONS.md)** and the build
order is [`docs/PLAN.md`](docs/PLAN.md). Those two files are the authority. If
the code and the spec disagree, the spec is right and the code is a bug.

## Commands

```bash
uv sync                      # install dependencies
uv run uvicorn kanakko.app:app --reload   # run locally
uv run pytest                # run the checks
docker compose up --build    # full stack (web + db + cron)
uv run python -m kanakko.migrate   # apply pending migrations (needs DATABASE_URL)
```

## Layout

```
kanakko/
  app.py            FastAPI app: webhook + Mini App routes
  parse.py          OpenRouter call, JSON schema, validation, retry
  categories.py     THE category list — single source of truth
  money.py          Decimal helpers
  db.py             psycopg connection + queries (plain SQL)
  jobs/             one module per scheduled job
migrations/         numbered .sql, applied in order, never edited once applied
prompts/            Ralph loop prompts (not application code)
tests/              pytest
```

## Conventions

**Money is `Decimal` and `NUMERIC(12,2)`. Never `float`.** Anything that
touches an amount gets a check. This is the one area where "it's probably
fine" is not acceptable.

**All timestamps are `timestamptz` stored in UTC.** Every day/month boundary is
computed `AT TIME ZONE 'Asia/Kolkata'`. Never store naive datetimes. Never
bucket a report in UTC.

**Reads go through the `active_transactions` view**, never `transactions`
directly — that view is what applies the soft-delete filter, and bypassing it
resurrects deleted rows inside totals.

**Categories come from `kanakko/categories.py` and nowhere else.** The JSON
schema `enum` and the Telegram keyboard are both generated from it. Never write
a category string literal in another module.

**Plain SQL via `psycopg`.** No ORM. No SQLAlchemy. If a query is getting
unwieldy, it's a view, not an ORM.

**No new dependency without a reason in the commit message.** The design
deliberately excludes an ORM, Celery, Redis, and any charting library —
see `docs/DECISIONS.md` §7, §8, §13 for why. Adding one of those back is a
decision, not an implementation detail.

**Secrets come from the environment.** Never a literal token in code, in
`docker-compose.yml`, or in a test. `.env.example` carries keys with empty
values.

## Testing

Not exhaustive unit coverage — a check exists where breakage would be silent:

- the money path (parse → store → sum by category), using `Decimal`
- timezone bucketing, especially a transaction at 23:50 IST on a month's last day
- `initData` HMAC validation, including that a forged payload is rejected
- the noon-nudge suppression rule

A test that only restates the implementation is not worth writing.

## What not to do

- Don't add placeholder or stub implementations. Finish the task or leave it
  unchecked.
- Don't edit `docs/DECISIONS.md` to match the code. If the spec is wrong, add a
  task saying so — a spec that drifts to match the implementation is not a spec.
- Don't edit a migration that has already been applied. Add a new one.
- Don't deploy. Deployment is manual and human-run (`docs/DEPLOYMENT.md`).
