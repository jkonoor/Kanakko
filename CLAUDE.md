# Kanakko

A personal finance tracker over Telegram: natural-language transaction entry,
one-tap confirmation, and a Telegram Mini App dashboard.

**[`docs/DECISIONS.md`](docs/DECISIONS.md) is the specification and the
authority.** If the code and the spec disagree, the spec is right and the code
is a bug. Never edit the spec to match the code — if the spec is wrong, say so
in `TASKS.md`. Build order is [`docs/PLAN.md`](docs/PLAN.md); commands and
layout are in [`AGENTS.md`](AGENTS.md).

## Guards and checks

Most rework on this project has come from one habit, so it gets stated first.

**A guard must fail for the reason it exists.** Before adding a check, name the
concrete breakage it prevents — then actually cause that breakage and watch the
check go red. A check that has never failed is a claim, not a guarantee.

**Assert the effect, not the spelling.** These are real findings from this
repo's own reviews:

- A guard asserted the string `TZ=Asia/Kolkata` was present. But `TZ` is a
  silent no-op if the image can't resolve the zone, so the guard passed while
  the 21:00 summary would have fired at 02:30 IST.
- A guard checked that `web` wasn't published on `0.0.0.0`. It passed on three
  ordinary ways to reintroduce the exposure — one of which published Postgres.
- Migration guards "covered the money paths" without the paths being in scope.

In each case the check asserted a surface form while the risk lived in a
behaviour. **A guard that reports safety it doesn't provide is worse than no
guard**, because it stops anyone looking.

**Parse structured formats; never regex them.** YAML, JSON, TOML, `.env` — use a
parser. A regex matches the spellings you thought of and passes on the ones you
didn't; that exact bug cost three passes here.

## Engineering conventions

**Money is `Decimal` and `NUMERIC(12,2)`. Never `float`.** Anything touching an
amount gets a check. This is the one area where "it's probably fine" is not
acceptable.

**All timestamps are `timestamptz` in UTC.** Every day/month boundary is
computed `AT TIME ZONE 'Asia/Kolkata'`. Never store naive datetimes; never
bucket a report in UTC.

**Reads go through the `active_transactions` view**, never `transactions`
directly — the view applies the soft-delete filter, and bypassing it resurrects
deleted rows inside totals.

**Categories come from `kanakko/categories.py` and nowhere else.** The JSON
schema `enum` and the Telegram keyboard are generated from it. Never write a
category string literal in another module.

**Plain SQL via `psycopg`.** No ORM. If a query gets unwieldy, that's a view.

**One definition per thing.** Two places that must agree will eventually
disagree — a command in both the Dockerfile and compose, a version in both
`pyproject.toml` and `__init__.py`. Put it in one place and derive the other.
This has already happened twice: `MODEL_DEFAULT` drifted from `DECISIONS.md` §2
while production ran a third model, and three Mini App routes each carried their
own copy of the auth preamble. **The second copy is a warning; the third is the
bug.** That applies to prose as well as code — three route docstrings re-explained
the same header scheme, and they drifted too.

**Files stay under 300 lines, split by responsibility rather than by layer.**
`webapp.py` reached 545 lines holding HMAC verification and CSS side by side, and
`app.py` 509 holding the HTTP surface, the update layer, and the Mini App routes.
Security code never shares a file with presentation. When a module is doing two
jobs, the seam is usually already visible in its own docstring.

`db.py` is the one file over that line (426) and is deliberately left alone: it is
17 small functions doing one job, and splitting by entity would be splitting by
layer. **Its trigger is Phase 9** — households, memberships and invites will push
it past 600, and that is the point to make `db/` a package. Splitting a file that
has one responsibility just to hit a number is how a codebase gets worse.

**A fan-out over users isolates failures per user.** One bad recipient must never
stop the rest, and must never roll back work that already succeeded — the jobs
looped without a `try`, so a single blocked user silenced everyone after them
*and* discarded the `reminder_log` rows written before them. Each user gets its
own savepoint; failures are collected and raised together at the end.

**No new dependency without a reason in the commit message.** The design
deliberately excludes an ORM, Celery, Redis, and any charting library — see
`docs/DECISIONS.md` §7, §8, §13. Adding one back is a decision, not an
implementation detail.

**Secrets come from the environment.** Never a literal token in code, in
`docker-compose.yml`, or in a test. `.env.example` carries keys with empty
values.

**Never publish a port on `0.0.0.0`.** This deploys to a host that is not ours
alone. Bind to `127.0.0.1`; Traefik reaches containers over the docker network.

## What not to do

- No placeholder or stub implementations. Finish the task or leave it unticked.
- Don't edit `docs/DECISIONS.md` to match the code.
- Don't edit a migration that has already been applied. Add a new one.
- Don't deploy, and don't call the Dokploy API. Deployment is human-run —
  see [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
