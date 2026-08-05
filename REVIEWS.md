# QA Reviews — Kanakko

The QA pass prepends the newest review here, one per implementer commit. The
implementer reads this file **first** each iteration and fixes any open
(⚠️ CHANGES REQUESTED) findings before starting new work.

Status markers: **✅ DONE** — no blocking issues · **⚠️ CHANGES REQUESTED** —
open findings the next iteration must fix before anything else.

A review records what was actually checked and what the commands actually
returned, not what they were assumed to return.

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
