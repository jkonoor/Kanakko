# Working on Kanakko

How to build, run, and test this project.

**Conventions and rules live in [`CLAUDE.md`](CLAUDE.md)** — read it before
writing code. It is loaded automatically in every session, and it is
authoritative on money, timezones, guards, dependencies, and secrets. This file
deliberately does not repeat it: two copies of a rule eventually disagree.

The specification is [`docs/DECISIONS.md`](docs/DECISIONS.md); the build order
is [`docs/PLAN.md`](docs/PLAN.md); the queue is [`TASKS.md`](TASKS.md).

## Commands

```bash
uv sync                                    # install dependencies
uv run uvicorn kanakko.app:app --reload    # run locally
uv run pytest                              # run the checks
uv run ruff check . --fix                  # lint (and fix what is safely fixable)
git config core.hooksPath .githooks        # once per clone: enable the pre-commit hook
docker compose up --build                  # full stack (web + db + cron)
uv run python -m kanakko.migrate           # apply pending migrations (needs DATABASE_URL)
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

The image is built by `.github/workflows/deploy.yml` and runs on Dokploy as
three services — see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Testing

Not exhaustive unit coverage. A check exists where breakage would otherwise be
**silent** — where the system keeps returning plausible answers while being
wrong:

- the money path (parse → store → sum by category), using `Decimal`
- timezone bucketing, especially a transaction at 23:50 IST on a month's last day
- `initData` HMAC validation, including that a forged payload is rejected
- the noon-nudge suppression rule

Automated checks are only half of it. [`docs/TESTING.md`](docs/TESTING.md) is the
manual plan — what a person verifies in Telegram on a phone, which no headless run
can see: whether a card renders, a reminder arrives, or a note overlaps its row.

A test that only restates the implementation is not worth writing. Neither is
one that asserts a string when the risk is a behaviour — see **Guards and
checks** in [`CLAUDE.md`](CLAUDE.md), which is where this project's rework has
mostly come from.

When fixing a bug, confirm the check earns its place: temporarily revert the
fix, watch the check fail, then restore it.
