# QA Reviews — Kanakko

The QA pass prepends the newest review here, one per implementer commit. The
implementer reads this file **first** each iteration and fixes any open
(⚠️ CHANGES REQUESTED) findings before starting new work.

Status markers: **✅ DONE** — no blocking issues · **⚠️ CHANGES REQUESTED** —
open findings the next iteration must fix before anything else.

A review records what was actually checked and what the commands actually
returned, not what they were assumed to return.

---

## 2026-08-05 — `4b92212` — `migrations/001_init.sql` + migration guard test

**Scope:** adds `migrations/001_init.sql` (four tables, two indexes, the
`active_transactions` view) and `tests/test_migrations.py` (three guards over
`migrations/*.sql`); ticks Phase 0 task 3 in `TASKS.md`.

**Status: ⚠️ CHANGES REQUESTED** — the DDL itself is correct and the tick is
honest. The findings are all in the guard test, which advertises protection on
the money path that I verified it does not provide.

> **RESOLVED** in the follow-up commit on `ralph/phase-0`. Findings 1, 2 and 4
> are fixed together by inverting the money guard: it no longer looks for
> columns named `*amount*` in `CREATE TABLE` bodies, it checks that **every**
> `NUMERIC` in every migration is `(12, 2)`, whitespace-normalised. Finding 3 is
> fixed by `test_active_transactions_view_hides_deleted_rows`. Finding 5 is
> carried onto the migration-runner task in `TASKS.md`, since the claim lives in
> an already-published commit message and the fix is to actually run the DDL.
>
> Each scenario re-run against a scratch migration, then the scratch removed:
> `ALTER COLUMN amount TYPE NUMERIC(12, 4)` → `NUMERIC(12,4)` assertion fires;
> `balance NUMERIC(12, 0)` → fires; bare `NUMERIC` → fires as
> `NUMERIC(unspecified)`; `CREATE OR REPLACE VIEW active_transactions AS SELECT
> * FROM transactions;` → view guard fires; `amount_paid NUMERIC(12,2)` and
> `opening NUMERIC (12, 2)` → both pass. `uv run pytest` on the restored tree:
> `5 passed`.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -v` | `4 passed, 1 warning in 0.26s` — `test_migrations_exist`, `test_amounts_are_numeric_never_float`, `test_timestamps_are_timezone_aware`, plus the pre-existing `test_healthz_reports_ok_and_version` |
| Object inventory vs `docs/PLAN.md` | `grep -n "CREATE TABLE\|VIEW\|INDEX" migrations/001_init.sql` | `users:4`, `transactions:11`, `pending_transactions:35`, `reminder_log:45`, `active_transactions:57`, two indexes. All four tables and the view the task named exist, with the columns `docs/PLAN.md` lists. Not a stub. |
| Guard catches float (commit's claim) | added `migrations/999_qa_scratch.sql` with `amount DOUBLE PRECISION`, `uv run pytest -q` | `1 failed, 3 passed` — `test_amounts_are_numeric_never_float`. Verified, not assumed. |
| Guard catches wrong scale + naive time | scratch file with `amount NUMERIC(12, 0)` and `created_at TIMESTAMP` | `2 failed, 2 passed` — both guards fired. Verified. |
| **Guard blind to `ALTER COLUMN … TYPE`** | scratch file with `ALTER TABLE transactions ALTER COLUMN amount TYPE NUMERIC(12, 4);` | **`4 passed`** — silent. See finding 1. |
| **Guard blind to money columns not named `*amount*`** | scratch file with `balance NUMERIC(12, 0)`, `total NUMERIC` | **`4 passed`** — silent. See finding 2. |
| Guard rejects the spec's own spelling | scratch file with `amount_paid NUMERIC(12,2)` | `1 failed` — `amount_paid is NUMERIC(12,2)`. See finding 4. |
| Scratch file removed, tree restored | `rm migrations/999_qa_scratch.sql`, `uv run pytest -q`, `git status --short` | `4 passed`, clean. No review artifacts left behind. |
| DDL executed against Postgres | — | **Not done.** `psql --version` was denied by this session's permissions. See finding 5 on the commit message's stated reason. |
| Secrets | `grep -cniE "password\|secret\|token\|api[_-]?key"` over both new files | `0` and `0`. No seed row, no credential, no environment-specific literal. |

**Spec conformance (read line by line against `docs/DECISIONS.md`):**

- §9 money — `amount NUMERIC(12, 2)` at `migrations/001_init.sql:16`. No
  `REAL`/`FLOAT`/`DOUBLE PRECISION` anywhere in the file. ✓
- §3 nullability — `amount … NOT NULL` (`:16`), `category TEXT` nullable
  (`:21`), no confidence column anywhere in the schema. ✓
- §6 soft delete — `deleted_at TIMESTAMPTZ` (`:26`) and the view at `:57–58` is
  character-for-character the DDL §6 specifies. ✓
- §10 time — every timestamp column is `TIMESTAMPTZ`; `occurred_on` is a `DATE`
  separate from `created_at`, which is what makes `AT TIME ZONE 'Asia/Kolkata'`
  bucketing possible later. No naive `TIMESTAMP`. ✓
- §11 categories — no category string literal in the DDL, and deliberately no
  `CHECK` on `category`. The commit's reasoning here is right and worth keeping.
  ✓
- §1 tenancy — `user_id BIGINT NOT NULL REFERENCES users (user_id)` on all three
  child tables. ✓
- §7/§8 — plain `.sql`, no ORM artefacts, no queue/broker tables. ✓
- The partial index at `:30–32` shares the view's `WHERE deleted_at IS NULL`
  predicate, so Postgres can use it for reads through `active_transactions`.
  Correct shape.

**On the tick in `TASKS.md`:** legitimate. Task 3 asked for the four tables and
the view per `docs/PLAN.md`; all five objects exist with real column
definitions, constraints and indexes. Nothing here is a placeholder.

### Findings

**1 — Medium. The money guard cannot see `ALTER COLUMN … TYPE`, which is the
only way the scale will ever change.** `tests/test_migrations.py:26`

The column-type regex is `^\s+(\w*amount\w*)\s+(\S+[^,\n]*)` — it requires the
column name to be the first token on an indented line, i.e. a `CREATE TABLE`
body. `AGENTS.md:82` forbids editing an applied migration, so the *only*
sanctioned way to change `amount`'s type is a new migration containing an
`ALTER`. That is exactly the form the guard skips.

*Failure scenario:* migration `007_widen_amount.sql` contains
`ALTER TABLE transactions ALTER COLUMN amount TYPE NUMERIC(12, 4);`. Every
stored amount silently gains two paise-sub-digits, category sums stop agreeing
with the confirm cards the user tapped, and `uv run pytest` reports `4 passed`.
I ran precisely this file and got `4 passed, 1 warning in 0.28s`.

*Suggested fix:* also scan for `ALTER COLUMN` — one more `re.findall` over
`ALTER\s+COLUMN\s+(\w*amount\w*)\s+(?:SET\s+DATA\s+)?TYPE\s+([^;,\n]+)` feeding
the same assertion.

**2 — Medium. The guard only inspects columns literally named `*amount*`.**
`tests/test_migrations.py:26`

A `NUMERIC` money column named `balance`, `total`, `income`, or `opening` is
never type-checked. The blanket `REAL|FLOAT|DOUBLE PRECISION` search at `:25`
catches the float case, but not a missing or wrong *scale*, which is the
quieter half of §9.

*Failure scenario:* a later migration adds `balance NUMERIC(12, 0)` (or bare
`NUMERIC`) to a summary table. Every balance rounds to whole rupees on insert —
Postgres rounds, it does not error — so a ₹1,234.56 balance stores as
`1235`. Verified: a scratch migration with `balance NUMERIC(12, 0)` and
`total NUMERIC` produced `4 passed`.

*Suggested fix:* assert on the type rather than the name — flag any
`NUMERIC(...)` in a migration whose precision/scale is not `(12, 2)`, and any
bare `NUMERIC`. That inverts the check from "columns I remembered to name
`amount`" to "every fixed-point column in the schema".

**3 — Medium. Nothing checks the §6 soft-delete invariant, the schema's
highest-value silent-failure surface.** `tests/test_migrations.py` (absent)

The guard file covers money and timestamps, both correctly identified in its
docstring as silent. The third member of that class — `deleted_at` plus the
`active_transactions` view — has no check at all, even though `DECISIONS.md:119`
says in as many words that a forgotten `WHERE` clause "can't resurrect deleted
rows inside a monthly total" *because* the view exists.

*Failure scenario:* migration `00N` runs
`CREATE OR REPLACE VIEW active_transactions AS SELECT * FROM transactions;` —
plausible when someone adds a column and reaches for `OR REPLACE` (see the note
below on `SELECT *`). Every soft-deleted row returns to every total, `/undo`
stops appearing to work, and the suite reports `4 passed`.

*Suggested fix:* one assert that `active_transactions` is defined `WHERE
deleted_at IS NULL` in whichever migration last defines it. Cheap, and it guards
the one invariant the spec calls non-negotiable insurance.

**4 — Low (loud, not silent). The guard asserts on formatting, and rejects the
spelling the spec itself uses.** `tests/test_migrations.py:27`

`type_.upper().startswith("NUMERIC(12, 2)")` requires the space after the comma.
`docs/DECISIONS.md:174`, `docs/PLAN.md`'s data model, and `AGENTS.md:37` all
write it as `NUMERIC(12,2)`.

*Failure scenario:* whoever writes migration 002 copies the type out of
`AGENTS.md` and the suite fails with
`amount_paid is NUMERIC(12,2) NOT NULL` — a correct column reported as a money
violation. Verified: that exact line produced `1 failed`. It fails loudly, so no
data is at risk; the cost is a confusing red build and the temptation to weaken
the assertion.

*Suggested fix:* normalise before comparing —
`re.sub(r"\s+", "", type_).upper().startswith("NUMERIC(12,2)")`.

**5 — Low. The commit message's stated reason for not executing the DDL does not
hold up.** commit `4b92212` message, final paragraph

It says "no psql, initdb, or docker is available in this environment".
`ls /usr/bin/psql` resolved to `/usr/share/postgresql-common/pg_wrapper` — the
Debian `postgresql-client` wrapper — before the sandbox blocked the listing, so
a psql client does appear to be installed on this host. I could not run it:
`psql --version` was denied by this session's permission settings, so whether a
*server* is installed or reachable is **UNKNOWN** to me, and the substantive
caveat stands — this DDL has still never been parsed by Postgres, and its first
execution will be the migration-runner task.

*Failure scenario:* the next iteration reads "not available", skips trying, and
a syntax error in 58 lines of unparsed DDL surfaces during the Dokploy deploy
instead of locally. `GENERATED ALWAYS AS IDENTITY` (PG 10+) and the partial
index are the two constructs worth having a real parser confirm.

*Suggested fix:* on the migration-runner task, attempt `psql -f` (or
`docker compose up db`) once and record the actual outcome rather than the
assumed one. If it is genuinely unavailable, say which command was run and what
it returned.

### Noted, not findings

**`CREATE VIEW active_transactions AS SELECT *`** (`:57–58`) freezes the view's
column list at creation time — a column added to `transactions` by a later
migration will not appear through the view, silently. This is not a finding
because `docs/DECISIONS.md:110–113` specifies this exact DDL, and `AGENTS.md:80`
forbids editing the spec to match the code. Flagging it so the migration that
first adds a `transactions` column remembers to recreate the view; that is the
same migration finding 3's guard would protect.

**`type TEXT NOT NULL CHECK (type IN ('expense', 'income'))`** (`:17`) puts two
strings in the DDL that will also exist in `kanakko/categories.py`, whose §11
structure is keyed by exactly those two directions. The commit refused a
`category` CHECK for precisely this reason. It is not a violation — §11 and
`AGENTS.md:49` govern *categories*, not the direction enum — and unlike the
category list this set is genuinely closed and will not churn. Worth one
conscious look when `categories.py` lands, not a change now.

The reasoning recorded in the commit message is unusually good: `CHECK (amount
> 0)` with direction in `type`, no `CHECK` on `category`, and no seed row in
`users` are each the right call and each explained. The gaps above are in what
the test can see, not in what the schema says.

---

## 2026-08-05 — `3dc8e6c` — FastAPI app + `/healthz`

**Scope:** adds `kanakko/app.py` (FastAPI instance, `GET /healthz`) and
`tests/test_app.py`; ticks Phase 0 task 2 in `TASKS.md`.

**Status: ✅ DONE** — no blocking issues. One minor, non-blocking observation
below; it is not a regression from this commit.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -v` | `1 passed, 1 warning in 0.26s` — `tests/test_app.py::test_healthz_reports_ok_and_version PASSED` |
| Endpoint live, not just under `TestClient` | `uv run uvicorn kanakko.app:app --port 8731`, then `GET /healthz` | `200` · body `{"status":"ok","version":"0.1.0"}` |
| Route surface | live probes | `POST /healthz` → `405`, `GET /nope` → `404`, `GET /healthz/` → `200` (via 307). No unintended handlers. |
| OpenAPI wiring | `GET /openapi.json` | `{'title': 'Kanakko', 'version': '0.1.0'}`, paths `['/healthz']` — `title`/`version` reach the schema, matching the app metadata |
| Task claim is not a stub | read `kanakko/app.py` body | 12 lines, real handler, real return value. Task 2 asked for "a `/healthz` endpoint returning 200 and the app version" — that is exactly what exists. Tick is honest. |
| Version not hardcoded (commit's central claim) | temporarily replaced `__version__` with `"0.0.0"`, ran `uv run pytest -q` | `1 failed` — `AssertionError`. The test does catch a hardcoded/drifting version. Claim verified, not assumed. |
| Test isn't a tautology on payload shape | temporarily added an `"extra": "x"` key to the response, ran `uv run pytest -q` | `1 failed`. The equality assertion catches payload drift too. |
| Working tree restored after mutation | `git status --porcelain`, `uv run pytest -q` | clean, `1 passed`. No review artifacts left behind. |
| Money path | `grep -rniE "float\|token\|secret\|api[_-]?key\|password"` over `*.py`/`*.toml`/`*.yml` | no matches. No amount handling exists yet in this commit, so no `Decimal`/`float` surface to violate. |
| Secrets | same grep, plus read of the diff | none. No `.env`, no literal token, no fixture credential. `.gitignore` already covers `.env` / `*.env` with a `!.env.example` exception. |

**Spec conformance:** this commit touches no money, no SQL, no timestamps, no
categories, no LLM prompt, and adds no dependency. The decisions most easily
violated silently (`Decimal`/`NUMERIC(12,2)`, `active_transactions`,
`AT TIME ZONE 'Asia/Kolkata'`, date injection, nullable `category` /
non-nullable `amount`, no confidence score, no ORM/Celery/Redis/charting/state
machine) have no surface here. Nothing to conform to yet, and nothing violated.
`kanakko/app.py`'s docstring matches the `AGENTS.md` layout entry for it.

### Findings

**1 — Minor, non-blocking. Version is declared in two places; `/healthz` can
report a stale build.** `kanakko/__init__.py:1` and `pyproject.toml:3` are two
independent `"0.1.0"` literals with nothing tying them together.

*Failure scenario:* someone bumps `pyproject.toml` to `0.2.0` for a release and
forgets `kanakko/__init__.py`. `/healthz` keeps answering `"version": "0.1.0"`,
so the Phase 0 human task — "deploy, attach domain, verify `/healthz` over
HTTPS" — verifies a green endpoint that names the wrong build. Nothing raises.
`tests/test_app.py:11` cannot catch this, because it asserts against
`kanakko.__version__`, the same constant the endpoint returns; both mutation
runs above confirm it only detects drift *within* that pair.

*Why it is not blocking:* the duplication predates this commit — `pyproject.toml`
arrived in `d63e5ff`. `3dc8e6c` is what makes the value externally visible, but
it did not introduce the split, and `app.py`'s own choice (read
`kanakko.__version__` rather than re-declare) is the right one. Verified the two
are currently in sync: `pyproject: 0.1.0 | __init__: 0.1.0 | in sync: True`.

*Suggested fix (one line, whenever convenient):* drop the literal from
`kanakko/__init__.py` and derive it —
`__version__ = importlib.metadata.version("kanakko")`. Confirmed working in this
venv: `md.version('kanakko')` returns `0.1.0`. That makes `pyproject.toml` the
single source and deletes the drift entirely. Alternatively point hatchling at
the module with `[tool.hatch.version] path = "kanakko/__init__.py"`.

### Noted, not a finding

`fastapi.testclient` emits `StarletteDeprecationWarning: Using httpx with
starlette.testclient is deprecated; install httpx2 instead`. Reproduced — it is
real and it is in the pytest output. The commit message already flags it and
leaves it alone, which is correct: it is a dependency-side warning, it does not
affect the assertion, and `httpx` is a declared dependency needed for the
OpenRouter call regardless.

No other issues. The one test present is the one worth having: it fails if the
version is wrong and it fails if the payload shape changes, and I verified both
by breaking the code rather than by reading it.

---

_Older reviews follow below._
