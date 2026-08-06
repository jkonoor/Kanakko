# QA Reviews — Kanakko

The QA pass prepends the newest review here, one per implementer commit. The
implementer reads this file **first** each iteration and fixes any open
(⚠️ CHANGES REQUESTED) findings before starting new work.

Status markers: **✅ DONE** — no blocking issues · **⚠️ CHANGES REQUESTED** —
open findings the next iteration must fix before anything else.

A review records what was actually checked and what the commands actually
returned, not what they were assumed to return.

---

## 2026-08-06 — `8ee86b9` — rewrite `test_today_reads_the_kolkata_clock` to verify the zone, not the format

**Scope:** test-only follow-up. Rewrites the one guard flagged ⚠️ on `b21aa45`
so it asserts the *behaviour* (the clock resolves in `Asia/Kolkata`) instead of a
surface form (the date *format*). Diff touches two files (+31/-10):
`tests/test_parse.py` (test rewritten, `+datetime/timezone`, `+from kanakko import
parse`) and `REVIEWS.md` (prior finding marked RESOLVED). No production code
changed.

**Status: ✅ DONE** — no blocking issues. The rewritten guard earns its place: I
reproduced the exact defect it exists to catch and confirmed it goes red, then
green on restore.

### What I checked (and what it returned)

- **`git show HEAD`** — confirmed the commit is test + REVIEWS only; `kanakko/`
  production code is untouched.
- **`uv run pytest -q`** → `44 passed, 1 warning`. Matches the commit message.
- **The guard fails for the reason it exists.** The claim to verify is "mutating
  `KOLKATA` to UTC reddens exactly this test." I edited
  `kanakko/parse.py:30` `ZoneInfo("Asia/Kolkata")` → `ZoneInfo("UTC")` and ran
  `pytest tests/test_parse.py::test_today_reads_the_kolkata_clock` →
  **1 failed**: `AssertionError: assert '2026-08-06' == '2026-08-07'`. Restored
  via `git checkout kanakko/parse.py` → **1 passed**. The guard genuinely
  reddens when the zone breaks; the prior finding is closed.
- **Why the format assertion no longer hides the bug.** The old test asserted
  `re.fullmatch(r"\d{4}-\d{2}-\d{2}", today())` (any zone's date matches) and
  `today() in build_request(...)` (both sides call `today()`, self-consistent).
  The new test freezes an instant — `2026-08-06 20:00 UTC`, already `2026-08-07`
  in IST — and asserts `today() == "2026-08-07"`, an exact value only the
  correct zone produces. Traced the monkeypatch: `FrozenDatetime.now(tz)` returns
  `fixed.astimezone(tz)`, `today()` calls `datetime.now(KOLKATA)`, so the pinned
  instant flows through the real `KOLKATA` constant — the thing under test.

### Findings

None. The commit does exactly what its message claims and the fix is verified by
reproduction, not assertion. The retained format regex on line 75 is now
redundant with the exact-value assert above it, but it is harmless and the commit
deliberately kept it — not a finding.

---

## 2026-08-06 — `b21aa45` — inject current `Asia/Kolkata` date into the parse prompt (§10)

**Scope:** `build_request` now prepends today's `Asia/Kolkata` date to the
system prompt, `today()` reads that date from `zoneinfo` (§10's "one function"
provision), `today_str` is injectable for tests; `tests/test_parse.py` gains two
guards; `TASKS.md` line 38 ticked. `git show --stat HEAD` confirms exactly those
three files (+47/-5). Judged against `docs/DECISIONS.md` §10 (current
`Asia/Kolkata` date injected into **every** LLM prompt; timezone read through one
function for the multi-user path) and the CLAUDE.md guard conventions.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — the
shipped code was already correct and matches §10, but one of the two new guards
asserted a surface form (date *format*) while claiming to verify the behaviour
that actually matters (the zone is `Asia/Kolkata`, not UTC). It stayed green
when the zone was broken.

> **RESOLVED** in the follow-up commit on `ralph/phase-1`.
> `test_today_reads_the_kolkata_clock` now monkeypatches `parse.datetime` to a
> frozen `FrozenDatetime` whose `now(tz)` returns `2026-08-06 20:00 UTC`
> converted into `tz` — an instant where UTC (`2026-08-06`) and IST
> (`2026-08-07`) fall on different calendar days — and asserts
> `today() == "2026-08-07"`. The format regex is kept. Verified it earns its
> place: mutating `KOLKATA = ZoneInfo("Asia/Kolkata")` → `ZoneInfo("UTC")` now
> reddens exactly this test (`today()` returns `2026-08-06`, `AssertionError`);
> restoring greens it. `uv run pytest -q` → `44 passed`.

### What I checked (and what it returned)

- **Whole suite green.** `uv run pytest -q` → `44 passed, 1 warning` (the lone
  warning is the pre-existing Starlette/httpx deprecation, unrelated).
- **Spec fit (§10).** The date reaches the system message verbatim, ahead of the
  user message; wording tells the model to resolve "yesterday"/"last Friday"
  against it. `today()` uses `ZoneInfo("Asia/Kolkata")` via stdlib `zoneinfo` —
  no new dependency, no naive datetime. The single `today()` function satisfies
  §10's "read the timezone through one function … later returns a per-user
  column" provision. Matches §10.
- **No money/`float`/`active_transactions`/SQL surface touched** by this diff —
  nothing to check there.
- **Guard #1 revert-verified.** Replacing the injected `system` with bare
  `_SYSTEM_PROMPT` → `test_current_kolkata_date_is_injected_into_the_prompt`
  and `test_today_reads_the_kolkata_clock` both **FAIL**
  (`2 failed, 4 passed`). So the "date reaches the prompt" behaviour is genuinely
  guarded.
- **Guard #2 defeat test — it does NOT guard its stated purpose.** I mutated
  `KOLKATA = ZoneInfo("Asia/Kolkata")` → `ZoneInfo("UTC")` and reran
  `tests/test_parse.py` → **`6 passed`**, all green. The zone is broken and every
  guard stays green.

### Findings

**1 — LOW — `tests/test_parse.py:60-63` `test_today_reads_the_kolkata_clock`
does not verify Kolkata; its docstring overclaims.**

The test's comment says today() "must be a real Kolkata date, not a naive UTC
one," but the assertions are `re.fullmatch(r"\d{4}-\d{2}-\d{2}", today())` plus
`today() in _system_content(build_request("x"))`. The regex passes for *any*
zone's date, and the second assertion is satisfied because both sides call the
same `today()` — it can never disagree with itself. **Failure scenario:** a
future edit sets `KOLKATA = ZoneInfo("UTC")` (or someone writes
`datetime.now().strftime(...)` with no tz). Between 18:30–24:00 UTC that yields
*yesterday's* IST date, so every "yesterday"/"today" the model resolves near IST
midnight is silently off by a day — exactly the §10 failure. This guard stays
green through all of it (verified above: UTC mutation → `6 passed`). The
production code is correct **today**; the risk is that the test advertises
protection it doesn't provide, so the next person trusts it.

**Suggested fix:** pin an instant that differs across the two zones and assert
the IST answer. e.g. monkeypatch `parse.datetime` (or inject a clock) to
`2026-08-06 20:00:00+00:00` — UTC date `2026-08-06`, IST date `2026-08-07` — and
assert `today() == "2026-08-07"`. That reddens the moment the zone is wrong.
Keep the existing format/injection assertions.

### Not findings

- Absence of Pydantic validation + retry (task 39) and the nullable-category
  refinement (task 40) — both boxes still unchecked, out of scope.
- `today_str` injectability is a test seam, not dead flexibility — it's the
  §10 "one function" seam and how guard #1 stays deterministic.

---

## 2026-08-06 — `262fc0d` — add `kanakko/parse.py` (OpenRouter call + schema)

**Scope:** New `kanakko/parse.py` (parse schema, request body, thin httpx call)
and `tests/test_parse.py` (4 body/schema checks, no network); `TASKS.md` line 37
ticked. Judged against `docs/DECISIONS.md` §2 (OpenRouter, `response_format`
json_schema, `require_parameters: true`, default `claude-opus-5`), §9 (money is
`Decimal`/`NUMERIC`, no float), §11 (categories from one source), and the
CLAUDE.md conventions. `git show --stat HEAD` confirms exactly those three files
(+142/-1). The commit explicitly defers date injection (task 38), Pydantic
validation + retry (task 39), and the nullable-category refinement (task 40) —
all three boxes remain unchecked, so their absence is out of scope, not a
finding.

**Status: ✅ DONE** — the module does what task 37 asked and matches §2; the
guards fail for the reasons they exist. No blocking findings.

### What I checked (and what it returned)

- **Whole suite green.** `uv run pytest -q` → `42 passed, 1 warning`. The lone
  warning is the pre-existing Starlette/httpx deprecation, unrelated to this diff.
- **Every §2/§9/§11 lever exercised directly** (script driving the real code):
  - `require_parameters` → `True`; `response_format.json_schema.strict` → `True`;
    `response_format.type` → `json_schema`. Matches §2.
  - `amount` schema type → `string` (§9: a JSON number would decode to `float`
    and lose paise; a string survives to `money.parse_amount`).
  - `category` enum → `schema_enum()` exactly (`== True`), so the model cannot
    invent a category (§11); no literal category strings in `parse.py`.
  - `required` → `['type','amount','category','date','note']`,
    `additionalProperties` → `False`. Consistent with the current pre-task-40
    schema (category still required; §3's nullable refinement is task 40).
  - No confidence score anywhere in the schema (§3). Confirmed.
- **The empty-env model-default guard genuinely guards (the revert-verified
  one).** With `OPENROUTER_MODEL=""`, `build_request()['model']` → `claude-opus-5`;
  the naive `os.environ.get("OPENROUTER_MODEL", MODEL_DEFAULT)` returns `''` for
  the same input — so the guard reddens on the exact bug it names. Unset → default;
  `anthropic/claude-sonnet-5` → passes through. `model or env or MODEL_DEFAULT` is
  correct.
- **`call()` fails closed without a key.** With `OPENROUTER_API_KEY` unset,
  `call('x')` raises `RuntimeError("OPENROUTER_API_KEY is not set")` before any
  network I/O. Key is read from env and sent as a `Bearer` header — no secret in
  the repo, fixture, or compose.

### Notes (non-blocking, for the tasks that own them)

- The schema requires `date` (YYYY-MM-DD) but no current date is injected yet, so
  `call()` today would ask the model to date a transaction with no clock — this is
  precisely task 38, deferred and unwired (no caller invokes `call()` until the
  webhook, task 41). Not a finding; flagged so task 38 isn't lost.
- `call()`'s POST/parse path has no test (no network by design). Its only logic is
  the key-missing raise (verified above) and a standard OpenRouter response
  unwrap; validation of the returned body is task 39. Acceptable for this scope.

---

## 2026-08-06 — `1271d4a` — cover parse → store → sum-by-category with `Decimal`

**Scope:** Test-only commit. Adds `test_parse_store_sum_by_category_stays_exact`
to `tests/test_migrate.py`, ticks the matching box in `TASKS.md` (line 36,
"Add a check covering parse → store → sum-by-category using `Decimal`"). Judged
against `docs/DECISIONS.md` §6 (reads through `active_transactions`) and §9
(money is `Decimal`/`NUMERIC(12,2)`), and the CLAUDE.md money/soft-delete rules.
`git show --stat HEAD` confirms exactly `TASKS.md` (+1/-1) and
`tests/test_migrate.py` (+53). No production code touched.

**Status: ✅ DONE** — the test is real, non-vacuous, and its guards fail for the
reasons they exist. No findings.

### What I checked (and what it returned)

- **Whole suite green.** `uv run pytest -q` → `38 passed, 1 warning`. The lone
  warning is a pre-existing Starlette/httpx deprecation, unrelated to this diff.
- **The new test actually runs, not skipped.**
  `uv run pytest tests/test_migrate.py::test_parse_store_sum_by_category_stays_exact -v`
  → `PASSED` (a real Postgres cluster booted; the `conn` fixture's skip did not
  fire on this machine).
- **The drift claim is true, so the equality is non-vacuous.**
  `0.10+0.20+0.30` → `0.6000000000000001`, `10.10+20.20+0.05` →
  `30.349999999999998`; both `!= Decimal("0.60")` / `Decimal("30.35")`. A `float`
  column or a Python-side float sum would therefore redden the `==` assertion,
  and separately `isinstance(0.6, Decimal)` is `False`, so the type assertion
  catches a float SUM return even if the value happened to land exact.
- **The soft-delete guard genuinely guards (mutation check).** I temporarily
  changed the `SELECT … FROM active_transactions` to `FROM transactions` and
  re-ran the one test → `1 failed`: the soft-deleted ₹999.99 Food row leaks in
  and Food becomes `1000.59 != 0.60`. Restored the file; `git status` clean.
  This confirms the §6 read-through-the-view rule is exercised, not just recited.
- **Sources of truth respected.** Categories come from
  `EXPENSE_CATEGORIES` in `kanakko/categories.py` (indices 0/1 = Food/Groceries),
  not string literals; amounts enter through `parse_amount`; the sum runs in
  Postgres over `NUMERIC(12,2)`.

### Notes (non-blocking, not findings)

- The test binds Food/Groceries to `EXPENSE_CATEGORIES[0]`/`[1]` by position. If
  someone reorders the tuple in `categories.py` the test still passes (it reads
  whatever names sit at 0/1), so it won't spuriously break — acceptable, and the
  category-ordering contract isn't this test's job.
- Coverage of the money path is now: single-row round-trip
  (`test_schema_stores_money_exactly`) + multi-row SUM/GROUP BY with soft-delete
  exclusion (this commit). Month/day bucketing `AT TIME ZONE 'Asia/Kolkata'` is
  still unwritten — but its task is unchecked, so that's not-yet-done, not a
  finding here.

---

## 2026-08-06 — `44b3096` — reject non-finite Decimals in `parse_amount`

**Scope:** Fix for finding 1 of the `9d7046b` review — `Decimal("nan")` slipped
past `parse_amount`'s `InvalidOperation` net and surfaced as an uncaught
`decimal.InvalidOperation` at the `amount <= 0` line instead of the documented
`ValueError`. Three files: `kanakko/money.py` (the `is_finite` guard + two
`demo()` nan cases), `tests/test_money.py` (nan/NaN/-nan added to the garbage
parametrize), `REVIEWS.md` (marked RESOLVED). `git show --stat HEAD` confirms
exactly those three. Judged against `docs/DECISIONS.md` §9 and the CLAUDE.md
money rule.

**Status: ✅ DONE** — the input-validation hole is closed; the guard fails for
the reason it exists; the §9 `Decimal`/`NUMERIC(12,2)` precision guarantee is
untouched. No new findings.

### What I checked (and what it returned)

- `git show HEAD` — the change is a single 5-line guard
  (`if not amount.is_finite(): raise ValueError`) inserted *after* `quantize`
  and *before* `amount <= 0` (`money.py:46-47`), plus two test/demo additions.
  Placement is correct: it sits on the one path every amount passes through,
  ahead of the comparison that was raising.
- `uv run pytest -q` → **37 passed** (was 34; +3, matching the three new nan
  parametrize cases). `uv run python -m kanakko.money` → `money demo ok`.
- **Guard earns its place (revert test).** Removed the two guard lines and ran
  `uv run pytest tests/test_money.py -q` → `3 failed, 13 passed`; the three
  failures are exactly `[nan]`, `[NaN]`, `[-nan]`, each
  `decimal.InvalidOperation` at `money.py:46` (the `<= 0` line after removal).
  Restored the guard → clean (`git diff --stat` empty), tests green again. The
  check fails for precisely the breakage it prevents.
- **Semantics spot-check.** Confirmed `Decimal("nan").quantize(...)` succeeds
  but `nan <= 0` raises `InvalidOperation` (an `ArithmeticError`, *not* a
  `ValueError`) — so pre-fix the documented `ValueError` contract in the
  docstring was violated. `Decimal.is_finite()` returns `False` for
  `nan/-nan/inf/-inf/Infinity`, so the guard also defensively covers infinities
  (though `Decimal("inf").quantize(...)` already raises `InvalidOperation` and
  is caught upstream, so infinities never reach the new line — no behaviour
  change there, just belt-and-braces). No `float` is introduced; amounts remain
  `Decimal` throughout.

### Findings

None. The fix is minimal, correct, and lands on the shared entry point rather
than a caller. Both the new test cases and the two `demo()` additions
(`"nan"`, `"NaN"`) are real guards, not surface-form assertions — verified by
watching them go red on revert.

---

## 2026-08-06 — `9d7046b` — add `kanakko/money.py` (₹ amounts as `Decimal`, float refused)

**Scope:** New `kanakko/money.py` (`parse_amount`, `format_amount`, `demo`),
new `tests/test_money.py`, one ticked box in `TASKS.md`. `git show --stat HEAD`
confirms exactly those three files. Judged against `docs/DECISIONS.md` §9.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one
input-validation hole in the money entry point (a documented `ValueError` path
actually raised an uncaught `decimal.InvalidOperation`). The §9 precision
guarantee itself was intact.

> **RESOLVED** in the follow-up commit on `ralph/phase-1`. `parse_amount` now
> rejects non-finite Decimals — `if not amount.is_finite(): raise ValueError`
> — after quantize and before the `<= 0` comparison that was signalling the
> uncaught `InvalidOperation`. So `"nan"`/`"NaN"`/`"-nan"` now surface as the
> documented `ValueError` through the single door, not as an `ArithmeticError`.
> `test_rejects_non_positive_and_garbage` gained the three `nan` spellings and
> `demo()` gained `"nan"`/`"NaN"`; both fail for the reason they exist —
> reverting the `is_finite` guard reddens exactly the three `nan` cases
> (`3 failed, 13 passed`, each `decimal.InvalidOperation` at the `<= 0` line),
> restoring greens them. `uv run pytest -q` → `37 passed` (was 34; +3).

### What I checked (and what it returned)

- `uv run pytest -q` → **34 passed** (was 21). Real, not a claim.
- **Guard earns its place:** removed the `isinstance(value, bool) or
  isinstance(value, float)` block and re-ran `tests/test_money.py` →
  `test_float_is_refused_at_the_door` failed with `DID NOT RAISE TypeError`
  (12 passed, 1 failed). Restored → 13 passed. The float/bool guard is load-
  bearing.
- `uv run python -m kanakko.money` → `money demo ok`.
- **§9 exactness:** confirmed `parse_amount("0.1") + parse_amount("0.2") ==
  Decimal("0.30")` and the sum-by-category path returns a `Decimal`. No `float`
  anywhere in the module. `bool` (int subclass) is correctly excluded.
- **Boundary:** `parse_amount("9999999999.99")` == `MAX_AMOUNT`;
  `"10000000000"` and `"1e12"` rejected as over-`NUMERIC(12,2)`. Correct.
- **False-tick check:** the `money.py` box is ticked and the code delivers it;
  the *separate* "parse → store → sum-by-category" box is correctly left
  unticked (store path not built). No false tick.
- Adversarial inputs: `"inf"`, `"+inf"`, `"Infinity"`, `"snan"`, `"1e-5"`,
  empty/whitespace, `"-5"`, `"abc"` all → `ValueError` (good). **But see below.**

### Findings

**1. (medium) `parse_amount("nan"/"NaN"/"-nan")` raises an uncaught
`decimal.InvalidOperation`, not the documented `ValueError`.**
`kanakko/money.py:39-43`.

- Scenario: `parse_amount("nan")`. `Decimal("nan")` is a *valid* Decimal (a
  quiet NaN), and `Decimal("nan").quantize(_PAISE, ...)` returns `NaN`
  **without raising**, so it slips past the `except InvalidOperation → ValueError`
  net on line 40-41. Execution reaches `if amount <= 0:` (line 43), and a
  `<=` comparison against a NaN `Decimal` *signals* `InvalidOperation`, which
  is uncaught and propagates out of the function.
- Why it matters: the docstring promises "Raises `ValueError` if the value is
  not a number," and `parse_amount` is billed as the single door every amount
  passes through. A caller doing `try: parse_amount(x) except ValueError:` to
  reject bad input will **not** catch this — it surfaces as an unhandled
  `decimal.InvalidOperation` (an `ArithmeticError`, not `ValueError`), i.e. a
  crashed handler / 500 instead of a clean "invalid amount." The garbage-
  rejection test (`test_rejects_non_positive_and_garbage`) claims to cover
  garbage but omits `"nan"`, so the hole is green.
- Verified live: `parse_amount("nan")` / `"NaN"` / `"-nan"` →
  `decimal.InvalidOperation`; `"snan"`, `"+inf"`, `"Infinity"` → `ValueError`
  (those fail loudly in `quantize`, NaN does not).
- Suggested fix: after quantize, reject non-finite explicitly, e.g.
  `if not amount.is_finite(): raise ValueError(f"not a valid amount: {value!r}")`,
  and add `"nan"`, `"NaN"` to the garbage parametrize so the guard fails for
  the reason it exists.

### Non-findings (checked, deliberately not flagged)

- `format_amount` uses Western thousands grouping (`₹1,000,000.00`) while
  `parse_amount` accepts Indian grouping on input. `docs/DECISIONS.md` gives no
  display-grouping requirement and its only example (`₹1,234.50`) is identical
  in both systems — cosmetic, not a finding.
- `parse_amount(Decimal(0.1))` (a Decimal built from a float elsewhere) is
  accepted but quantized to `0.10` — the float itself is refused at the door;
  a Decimal input is rounded to paise as designed. No precision leak.

---

## 2026-08-06 — `b948736` — add `kanakko/categories.py`, the closed category set defined once

**Scope:** New `kanakko/categories.py` (the §11 expense/income lists as one
constant, plus `schema_enum()` and `keyboard(txn_type)` derived from it), new
`tests/test_categories.py`, and one ticked box in `TASKS.md`. `git show --stat
HEAD` confirms exactly those three files. Pure constants + two derivation
helpers — no money, no timestamps, no SQL, no LLM prompt, no `active_transactions`
read — so the `Decimal`/`NUMERIC`, `AT TIME ZONE 'Asia/Kolkata'`, and soft-delete
decisions have no surface here.

**Status: ✅ DONE** — the constant matches the spec verbatim, both helpers derive
from it, and the guard fails for the reason it exists. No blocking issues.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Categories match spec | `grep -A40 '## 11' docs/DECISIONS.md` vs the module | verbatim match — `Food · Groceries · Transport · Shopping · Bills & Utilities · Health · Entertainment · Other`; income `Salary · Freelance · Refund · Other` |
| Suite green | `uv run pytest -q` | `21 passed, 1 warning in 1.08s` (was 18) |
| Guard fails for its reason | renamed `Groceries`→`groceries` in the source, ran `pytest tests/test_categories.py` | `3 failed` (`_match_the_spec_verbatim`, `_deduped_union`, `_keyboard_mirrors`); restored → green |
| `schema_enum()` dedupes `Other` | `uv run python -c` calling `schema_enum()` | 11 entries, `Other` appears once — the two lists' shared `Other` collapses via `dict.fromkeys` |
| Keyboard derivation | `keyboard('expense')`/`keyboard('income')` live | two buttons per row, `callback_data` = `cat:<name>`, labels mirror the constant in order |
| Dependency already declared | `grep telegram pyproject.toml` | `python-telegram-bot>=21.10` already a project dep — the `telegram` import adds nothing new |

Note on method: after the sed-revert of the `Groceries` typo, a same-second
`git checkout` left a stale `__pycache__` `.pyc`, so an intermediate live probe
printed `groceries` against a source that already read `Groceries`. Touching the
source forced a recompile and the enum came back correct — flagging it only so the
transcript isn't misread as a real defect. The file on disk is clean (`grep`
confirms `Groceries`, `git status` clean).

### Findings

None blocking. Two non-blocking observations for whoever wires this up next:

1. **`keyboard('bogus')` raises `KeyError`, not a domain error.** `categories.py:44`
   — `CATEGORIES_BY_TYPE[txn_type]` bare-indexes. Acceptable now: `txn_type` will
   come from the parse schema's own `type` enum (`expense`/`income`), so an invalid
   value can't reach here from validated LLM output. Worth a guard only if a
   future caller passes an unvalidated string; not a fix for this commit.

2. **`ALL_CATEGORIES` order is expense-first.** The `enum` handed to the model is
   `[…expenses…, Salary, Freelance, Refund]`. §2/§3 don't constrain order and the
   model matches on value not position, so this is fine — noting only that the
   income-only categories sit at the tail, which is what the test asserts.

The ticked box in `TASKS.md` is honest: the file exists, both helpers do real
work, and the test transcribes §11 by hand rather than importing it — so a drift
between spec and constant goes red instead of silently agreeing with itself.

---

## 2026-08-06 — `55db09f` — wire the three app keys into compose, parse `.env` instead of regexing it

**Scope:** Resolves the two open findings from the `a69544e` review.
`docker-compose.yml` (+3 keys in `x-app-env`), `tests/test_compose.py` (parser
`_env_pairs`, `COMPOSE_VARS`, reverse guard), `REVIEWS.md` (marked RESOLVED).
`git show --stat HEAD` confirms exactly those three files. No application code, no
migration, no ticked box moved — so money/`Decimal`, `AT TIME ZONE
'Asia/Kolkata'`, `active_transactions`, and the no-ORM/Celery/Redis decisions have
no surface here.

**Status: ✅ DONE** — both findings genuinely closed; the guards fail for the
reasons they exist. One non-blocking note for whoever writes the Phase 2 reader.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite green | `uv run pytest -q` | `18 passed, 1 warning in 1.07s` (was 17) |
| Parser catches all four bypass spellings | `_env_pairs` over `export KEY=…`, indent, spaces-around-`=`, and the shadowed-empty duplicate | all four → `CAUGHT` (the shadow case yields both `('SECRET','realtoken')` and `('SECRET','')`, so the valued line still asserts) |
| Reverse guard non-vacuous | Removed the three added lines from a copy of compose and recomputed | `unconsumed after revert: ['OPENROUTER_API_KEY','OPENROUTER_MODEL','TELEGRAM_BOT_TOKEN']` — goes red, as claimed |
| `$$VAR` escape excluded from `COMPOSE_VARS` | `re.findall(r'(?<!\$)\$\{?(\w+)', 'test $$POSTGRES_USER end')` | `[]` — the healthcheck's escaped literal is correctly not counted as a consumer |
| Both directions balance | `COMPOSE_VARS` vs `ENV_KEYS` | identical set of six; `web` and `cron` both inherit `*app-env`, so all three new keys reach a container |

### Findings from the prior review — both closed

1. **Three keys wired.** `x-app-env` now carries `TELEGRAM_BOT_TOKEN` and
   `OPENROUTER_API_KEY` as `:?see .env.example` (a missing one stops `up`) and
   `OPENROUTER_MODEL` as `:-` (`docker-compose.yml:18-21`). Both `web` and `cron`
   inherit the anchor, so the operator-set keys reach a running container instead
   of surfacing as a `KeyError` in Phase 1. The new
   `test_env_example_lists_no_key_no_service_consumes` closes the reverse
   direction the old suite never checked, and it is non-vacuous (verified above).

2. **`.env` parsed, not regexed.** `_env_pairs()` replaces the anchored
   `dict(re.findall(...))`; it strips lines, drops an optional `export `, splits
   on the first `=`, and yields a **list** so a shadowed value still fails. All
   four bypass spellings from the prior review's table are now caught (verified
   above). This matches the CLAUDE.md rule "parse structured formats; never regex
   them."

### Non-blocking note (for the Phase 2 implementer, not this commit)

- **`OPENROUTER_MODEL: "${OPENROUTER_MODEL:-}"` makes the key always present as
  the empty string** (`docker-compose.yml:21`). Compose interpolation always sets
  the key; `:-` only substitutes an empty default, it does not leave the variable
  unset. So inside the container `OPENROUTER_MODEL=""`, never absent. DECISIONS §2
  says unset ⇒ `claude-opus-5`. A reader written as
  `os.environ.get("OPENROUTER_MODEL", "claude-opus-5")` would return `""` and the
  default would never fire — a silent wrong model. The fix belongs in the reader
  when it lands: `os.environ.get("OPENROUTER_MODEL") or "claude-opus-5"`. Nothing
  reads it yet (grep found the name only in `.env.example`, `docker-compose.yml`,
  and the tests), so this is a caveat, not a finding against `55db09f`.
- **`COMPOSE_VARS` matches `$VAR` inside compose comments too.** A future
  `.env.example` key mentioned only in a compose *comment* with a `$` prefix would
  satisfy the reverse guard without any service actually consuming it. No such
  comment exists today, so the guard is sound now; worth knowing before someone
  adds a `# uses $FOO` comment. Low severity.

---

## 2026-08-05 — `a69544e` — `.env.example` with every required key, plus two guards over it

**Scope:** Phase 0 task 6. `.env.example` (new, 27 lines), `tests/test_compose.py`
(+`ENV_EXAMPLE`/`ENV_KEYS` at module scope, +2 checks), `TASKS.md` line 26 ticked.
`git show --stat HEAD` confirms exactly those three files; `docker-compose.yml` is
untouched.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one
blocking finding and one that matters before a real secret gets pasted into
this file.

> **RESOLVED** in the follow-up commit on `ralph/phase-1`. Both findings.
>
> Finding 1: `x-app-env` now carries `TELEGRAM_BOT_TOKEN` and
> `OPENROUTER_API_KEY` (`:?see .env.example`, required — a missing one stops
> `up`) and `OPENROUTER_MODEL` (`:-`, since §2 defaults it). So `web` and `cron`
> both get all three, and the operator-set keys reach a container instead of
> surfacing as a `KeyError` in Phase 1. A new guard,
> `test_env_example_lists_no_key_no_service_consumes`, checks the reverse
> direction the old suite never did: every key in `.env.example` is interpolated
> by some service (`COMPOSE_VARS`) or is compose-derived (`DATABASE_URL`).
> `COMPOSE_VARS` matches `${VAR}` and bare `$VAR` but not `$$VAR` (the
> healthcheck's escaped literal), closing the note about the bare-`$` blind spot.
> Confirmed non-vacuous: stripping the three keys back out reddened it with
> `OPENROUTER_API_KEY is in .env.example but no service consumes it`.
>
> Finding 2: the anchored `dict(re.findall(...))` is gone. `_env_pairs()` strips
> each line, skips blanks/comments, drops an optional `export `, splits on the
> first `=`, strips both sides, and yields a **list** of pairs (`ENV_PAIRS`) so a
> value shadowed by a later empty duplicate is still asserted on.
> `test_env_example_holds_no_values` iterates `ENV_PAIRS`; the vacuity assertion
> now guards `ENV_PAIRS`. All four bypass spellings from the review's table —
> `export KEY=…`, a leading indent, spaces around `=`, and the shadowed
> duplicate — go red (`1 failed` each), verified by mutating the real file and
> restoring it. `git status --porcelain` clean after.
>
> No box moved — Phase 0's `.env.example` task was already ticked; only the
> compose contract and the guards were incomplete. `uv run pytest -q` →
> `18 passed, 1 warning` (was 17; +1 for the reverse guard).

The file itself is good and the commit message is honest about the three
mutations it claims: I re-ran all three against the real file and got the same
counts and the same `AssertionError` strings, quoted below. No secret is added,
every key is empty, the `POSTGRES_PASSWORD` URL-safety constraint from the
review of `4341744` is carried onto the key where the value is actually chosen,
and `OPENROUTER_MODEL`'s documented default matches `DECISIONS.md` §2 verbatim
("Default model `claude-opus-5`"). The judgement not to invent a webhook-secret
or allowed-user key is correct — I grepped `docs/DECISIONS.md` for
`env|secret|token|webhook|allow.?list|single.user` and the spec decides neither;
§1 populates `user_id` with a constant rather than reading it from anywhere.

What is wrong is the direction the new guard doesn't check. Three of the six
keys have no route into any container, and the guard is structured so that it
never could notice.

Nothing here touches application code, so money/`Decimal`, `AT TIME ZONE
'Asia/Kolkata'`, `active_transactions`, the injected current date, and the
no-ORM/no-Celery/no-Redis decisions have no surface in this commit.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite green | `uv run pytest -q` | `16 passed, 1 warning in 1.00s` (was 14) |
| Which env vars each service actually gets | `yaml.safe_load` on the real `docker-compose.yml`, printing `environment` and `env_file` per service | `db` → the three `POSTGRES_*`; `web` → `{DATABASE_URL}` only; `cron` → `{DATABASE_URL, TZ}`; `env_file` is `None` for all three |
| Every `${VAR}` in the compose file | `re.findall(r'\$\{(\w+)', COMPOSE)` | `['POSTGRES_DB', 'POSTGRES_PASSWORD', 'POSTGRES_USER']` — nothing else |
| Bare `$VAR` interpolation in the compose file | `re.findall(r'(?<!\$)\$(\w+)', COMPOSE)` | `[]` |
| Are the other three keys named anywhere in compose? | substring test for each | `TELEGRAM_BOT_TOKEN: False`, `OPENROUTER_API_KEY: False`, `OPENROUTER_MODEL: False` |
| `env_file:` anywhere in compose | substring test | `False` |
| Is `.env.example` really committed and un-ignored? | `git ls-files --error-unmatch .env.example`; `git check-ignore -v .env.example` | tracked; `not ignored` |
| Spec's default model | `grep -n -i model docs/DECISIONS.md` | line 29 — "Default model `claude-opus-5`" — matches the file's comment |
| Spec decides a webhook secret / allowlist? | `grep -n -iE 'env\|secret\|token\|webhook\|allow.?list\|single.user' docs/DECISIONS.md` | 6 hits, none of them an env key; §13 line 261 uses the bot token for `initData` HMAC |
| Secret in the diff | `git show HEAD` read in full | none — all six values empty |

**Mutation battery.** I mutated the real `.env.example`, ran `-k env_example`,
and restored from an in-memory copy each time. `git status --porcelain` was
empty afterwards and a content comparison against the original returned `True`.
The first three rows are the commit's own table, re-run independently; the rest
are mine.

| Mutation | Result |
|---|---|
| baseline | `2 passed` |
| `POSTGRES_DB=` line deleted | `1 failed` — `AssertionError: POSTGRES_DB is interpolated by compose but absent` |
| `TELEGRAM_BOT_TOKEN=123456:AAHrealtoken` | `1 failed` — `AssertionError: TELEGRAM_BOT_TOKEN carries a value` |
| every `KEY=` commented out | `2 failed` — the second `AssertionError: .env.example has no keys — the guard would pass vacuously` |
| `export TELEGRAM_BOT_TOKEN=123456:AAHrealtoken` | **`2 passed`** |
| `··TELEGRAM_BOT_TOKEN=123456:AAHrealtoken` (two-space indent) | **`2 passed`** |
| `OPENROUTER_API_KEY = sk-or-v1-real` (spaces around `=`) | **`2 passed`** |
| `TELEGRAM_BOT_TOKEN=123456:AAHrealtoken` followed by a second `TELEGRAM_BOT_TOKEN=` | **`2 passed`** |

The commit's three claims reproduce exactly. The last four rows are finding 2.

I could not run `docker compose config` — the command was denied in this
environment — so finding 1 rests on the compose file's own text rather than on
Docker's rendering of it. That is sufficient: the file declares no `env_file`
and names those three keys in zero `${...}` references, so there is no route
this file provides for them to reach a process, whatever Compose does with the
ones it does name.

### Findings

**1 — Three of the six keys never reach any container; the new guard checks only
the direction that cannot catch it.** `docker-compose.yml:8-12` (`x-app-env`),
`.env.example:1`, `tests/test_compose.py:73-81`.

`.env.example` line 1 states "Every key the stack needs. Copy to .env for local
`docker compose up`", and `docs/DEPLOYMENT.md:42` step 3 says
`compose.saveEnvironment` carries "bot token, OpenRouter key, DB credentials".
But `x-app-env` sets `DATABASE_URL` and nothing else, `cron` adds only `TZ`, no
service declares `env_file`, and `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY` and
`OPENROUTER_MODEL` appear nowhere in the compose file — verified by substring
test and by the `${VAR}` enumeration above, which returns the three `POSTGRES_*`
names only.

So the commit message's "docker-compose.yml untouched — it already names every
one of these keys" is not true: it names three of six.

Failure scenario, and it is the quiet kind. The operator fills in all six —
locally in `.env`, on Dokploy in `compose.saveEnvironment` per DEPLOYMENT step 3
— and `docker compose up` succeeds, because the only keys carrying `:?see
.env.example` are the three Postgres ones that *are* wired. `db` comes up
healthy, `web` migrates, `/healthz` returns 200, the domain gets a certificate,
and Phase 0 is signed off as deployed. The gap surfaces later, in Phase 1, the
first time `kanakko/parse.py` reads `OPENROUTER_API_KEY` or the webhook handler
reads `TELEGRAM_BOT_TOKEN` for the `initData` HMAC (`DECISIONS.md` §13, line
261) — as a `KeyError`/`None` inside the container against an environment the
operator can see, correctly set, in the Dokploy UI. The evidence points at the
application; the cause is a compose file that never asked for the value.

`test_env_example_lists_every_key_compose_interpolates` guards
compose → `.env.example`. The reverse — a key the operator is told to set that
no service consumes — is the one this repo actually has, and it is unguarded.
The docstring calling `.env.example` "the only list of what the operator has to
set" reads as if both directions were covered.

Suggested fix: add the three to `x-app-env` so `web` and `cron` both get them —

```yaml
  TELEGRAM_BOT_TOKEN: "${TELEGRAM_BOT_TOKEN:?see .env.example}"
  OPENROUTER_API_KEY: "${OPENROUTER_API_KEY:?see .env.example}"
  OPENROUTER_MODEL: "${OPENROUTER_MODEL:-}"
```

`OPENROUTER_MODEL` takes `:-` rather than `:?` precisely because §2 gives it a
default; the other two are required and should stop the deploy, not the first
webhook. Then invert the guard: every key in `ENV_KEYS` is either interpolated
by compose or explicitly listed as compose-derived. `DATABASE_URL` is the only
member of that second set today and is already commented out, so the exemption
list is empty — which is the point.

**2 — The committed-secret guard is bypassed by four ordinary `.env`
spellings.** `tests/test_compose.py:21`.

`ENV_KEYS = dict(re.findall(r"^(\w+)=(.*)$", ENV_EXAMPLE, re.M))` is anchored at
column zero, requires `\w` immediately, requires no space around `=`, and
collapses duplicates with `dict()` (last wins). All four mutations in the table
above put a token-shaped value into the committed file and both guards returned
`2 passed`:

- `export TELEGRAM_BOT_TOKEN=<real>` — the `export ` prefix is how a `.env` gets
  written when someone also `source`s it, and it is the most likely of the four
  to happen by habit rather than by accident.
- a leading indent, e.g. from a paste that carried whitespace.
- `OPENROUTER_API_KEY = sk-or-v1-real` — spaces around `=`.
- a duplicate key where the valued line comes first and an empty one follows;
  `dict()` keeps the last, so the guard reads `''` and the secret above it is
  invisible.

The failure is exactly the one the docstring names: the value is committed and
"nothing about the diff looks different from the placeholder it replaced" — and
now the guard agrees with the diff. This is the same shape as findings 1 and 2
on `c214544`: a regex enumerating spellings over a config file, which took two
QA rounds to close for the published-port guard before `be74462` replaced it
with the parser. The lesson is one commit old.

Suggested fix: stop enumerating. Take every line that is non-blank and does not
start with `#`, strip an optional `export `, split on the first `=`, strip both
sides, and keep a **list** of pairs rather than a dict so a shadowed duplicate
still gets asserted on. Roughly:

```python
def _pairs(text):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        yield key.strip(), value.strip()

ENV_PAIRS = list(_pairs(ENV_EXAMPLE))
ENV_KEYS = {k for k, _ in ENV_PAIRS}
```

Then `test_env_example_holds_no_values` iterates `ENV_PAIRS`, and the vacuity
assertion still works. All four rows above go red; the three the commit already
covers stay red.

### Notes, not findings

- `.gitignore:3`'s `!.env.example` is doing nothing: `*.env` does not match
  `.env.example` (the name ends in `.example`), and `.env` matches only itself.
  `git check-ignore -v .env.example` returns `not ignored` with no matching
  rule. Harmless, but `.env.example:5` and the docstring at
  `tests/test_compose.py:85` both cite the negation as load-bearing, so the
  reasoning is worth correcting if either is touched.
- `re.findall(r"\$\{(\w+)", COMPOSE)` misses Compose's bare `$VAR` form. Not a
  live problem — I checked, the file uses none — but if finding 1's fix is
  written with `$TELEGRAM_BOT_TOKEN`, the guard goes quiet on it.
- `TASKS.md:26` is legitimately ticked for the file it names. Finding 1 is not a
  false tick on this task; it is an incomplete deploy contract that this commit
  made visible by writing down the keys.

---

## 2026-08-05 — `be74462` — published-port guard moved from regex to the YAML parser

**Scope:** findings 1 and 2 from the review of `c214544`. `tests/test_compose.py`
(guard rewritten), `pyproject.toml` + `uv.lock` (pyyaml, dev group),
`REVIEWS.md` (prior review marked RESOLVED). No box ticked in `TASKS.md`,
`docker-compose.yml` untouched — `git show --name-only` confirms exactly those
four files.

**Status: ✅ DONE** — no blocking issues. Both findings are genuinely closed,
and I confirmed they were real by checking out the parent's guard and watching
it go green on the same mutations. Every one of the nine mutation results in
the commit message reproduced verbatim, including the `AssertionError` text.
Three minor notes below; none of them is a way for a public bind to reach the
host through this file, and none blocks the next task.

Moving to `yaml.safe_load` is the right call rather than a third regex patch.
The previous two guards were reimplementing a YAML parser badly, and each round
of QA found another spelling they had not anticipated. The parser sees what
Docker sees, so the spelling class is closed by construction rather than by
enumeration, and finding 2 disappears because there is no service list left to
go stale. `pyyaml` in the dev group is justified in the commit message per
`AGENTS.md`, is test-only, and is not one of the dependencies `DECISIONS.md`
§7/§8/§13 excludes.

Nothing in this commit touches application code, so the money, timezone,
`active_transactions`, and categories decisions have no surface here.
`git show HEAD | grep -inE 'password|token|secret|api[_-]key'` returns one hit,
the word `POSTGRES_PASSWORD` in the commit message prose. No secret added.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite green | `uv run pytest -q` | `14 passed, 1 warning in 1.03s` |
| Lock consistent with `pyproject.toml` | `uv lock --check` | `Resolved 28 packages`, no error |
| pyyaml is dev-only | `sed -n '1,25p' pyproject.toml` | absent from `[project].dependencies`, present in `[dependency-groups].dev` |
| Compose still parses | `yaml.safe_load` on the real file | `services` = `cron, db, web`; `web` → `['127.0.0.1:8000:8000']`, `db`/`cron` → `None` |
| Files in commit | `git show --name-only --format= HEAD` | `REVIEWS.md`, `pyproject.toml`, `tests/test_compose.py`, `uv.lock` |
| `DECISIONS.md` §14 supports the loopback rationale | `sed -n '291,320p' docs/DECISIONS.md` | "Innogenio `doc-panel` Dokploy … company Dokploy instance" — shared host, rationale holds |

**Mutation battery.** I mutated the real `docker-compose.yml`, ran the single
guard, and restored from an in-memory copy; `diff` against a `/tmp` backup was
`IDENTICAL` and `git status --porcelain` was clean after every run. The first
nine rows are the commit's own table, re-run independently; the last two are
mine.

| Mutation | rc | Message |
|---|---|---|
| baseline | 0 | `1 passed` |
| `- "5432:5432"  # debug` on `db` (**finding 1**) | 1 | `AssertionError: db: 5432:5432` |
| same, unquoted | 1 | `AssertionError: db: 5432:5432` |
| new `pgadmin` service, `- "5050:80"` (**finding 2**) | 1 | `AssertionError: pgadmin: 5050:80` |
| four-space list item, `- "0.0.0.0:8000:8000"` | 1 | `AssertionError: web: 0.0.0.0:8000:8000` |
| `- 8000:8000` | 1 | `AssertionError: web: 8000:8000` |
| `- 8000` (bare, parses to int) | 1 | `AssertionError: web: 8000` |
| long syntax `target`/`published` | 1 | `AssertionError: web: {'target': 8000, 'published': 8000}` |
| `ports:` block deleted | 1 | `AssertionError: no published port found …` |
| `- "[::1]:8000:8000"` (mine) | 1 | `AssertionError: web: [::1]:8000:8000` |
| `- "127.0.0.1:8000-8001:8000-8001"` (mine) | 0 | `1 passed` — correct, a loopback range |

**Does the fix actually fix something?** I restored the parent's guard
(`git show HEAD~1:tests/test_compose.py`) over the new one and re-ran the same
mutations:

| Mutation, old guard | rc | Result |
|---|---|---|
| `- "5432:5432"  # debug` on `db` | 0 | **`1 passed`** — finding 1 confirmed real |
| new `pgadmin`, `- "5050:80"` | 0 | **`1 passed`** — finding 2 confirmed real |
| four-space `- "0.0.0.0:8000:8000"` | 1 | `1 failed` |

So the parent's suite was green while `db` published 5432 on `0.0.0.0` with the
credentials from `.env`. That is exactly the scenario the review claimed, and
the new guard fails on it.

**Bypass probes** (publish publicly *without* a non-loopback `ports` entry):

| Probe | Result |
|---|---|
| `ports` moved to a top-level anchor merged in with `<<: *pub` | `1 failed` — parser resolves the merge, caught |
| long syntax with `host_ip: 0.0.0.0` | `1 failed` — caught |
| `network_mode: host` added to `web` | **`6 passed`** — not caught (note 1) |
| invalid YAML (tab) | `1 error` at collection — loud |
| top-level `services:` renamed | `1 error` at collection — loud |
| `web:` body emptied | `3 failed, 3 passed`, `AttributeError` — loud |

The three malformed-file cases fail loudly rather than silently, which is the
behaviour that matters; that the module-level `yaml.safe_load` takes the other
five assertions down with it is acceptable, since a compose file that does not
parse is not a file whose other properties are worth asserting.

### Notes (none blocking)

**1. `network_mode: host` is the one remaining way past the guard.**
`tests/test_compose.py:52`. Adding `network_mode: host` to `web` puts every
listening port on the host's public interfaces with no `ports:` key at all, and
the suite reports `6 passed`. This is the same one-line-edit shape the docstring
names as the threat, and `DECISIONS.md` §14's shared host is the same host. It
is a pre-existing blind spot, not a regression — the regex guards missed it too
— and the artifact does not use it, which is why this is a note rather than a
finding. Closing it is one line inside the existing loop:
`assert "network_mode" not in svc, f"{name}: network_mode bypasses the port guard"`.

**2. A correctly-written long-syntax loopback bind now fails with a misleading
message.** `tests/test_compose.py:59`. `- target: 8000 / published: 8000 /
host_ip: 127.0.0.1` is the documented compose spelling of a loopback publish and
is safe, but `str(dict)` never starts with `127.0.0.1:`, so it fails with
`AssertionError: web: {'target': 8000, …, 'host_ip': '127.0.0.1'}` — verified.
This errs in the safe direction, so it costs nothing today. What was lost is the
parent guard's explicit `f"{name}: use short syntax with 127.0.0.1"`, which told
the reader that short syntax is a requirement rather than leaving them to
conclude their correct config is unsafe. Worth restoring as a branch in the loop
or a sentence in the docstring, whenever this file is next open.

**3. The `found` assertion message names `web` but fires for any service.**
`tests/test_compose.py:61`. `"no published port found — web's ports: block is
gone"` is accurate today because `web` is the only publisher, but the loop it
guards is over every service, so the message will misdirect the first time the
publish lives elsewhere. Cosmetic; mentioned only because the whole point of
this file is that its failures are read by someone who has not been here before.

### Not findings

- The old `assert "target:" not in block` is gone, but long syntax is still
  rejected — through `str(published)` rather than a separate assertion. Verified
  above.
- pyyaml is a new dependency, but `AGENTS.md` asks for a reason in the commit
  message and there is one, it is dev-group only, and it is none of the excluded
  four.
- `REVIEWS.md` editing the prior review's status line to
  `⚠️ CHANGES REQUESTED → ✅ RESOLVED` is the convention this file already
  follows, not a rewrite of history — the original text is intact beneath it.

---

## 2026-08-05 — `c214544` — loopback guard widened to every service and spelling

**Scope:** finding 1 from the review of `e1032a1`. `tests/test_compose.py`
(guard replaced), `REVIEWS.md` (prior finding marked RESOLVED). No box ticked,
`docker-compose.yml` untouched.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one finding, again about the guard rather
than the artifact. The rewrite is a genuine improvement and every claim in the
commit message reproduced verbatim, including all four mutation results. But
the guard is still not closed: `- "5432:5432"  # debug` on `db` — a quoted
port with a trailing comment, which is what someone actually types when they
open Postgres to poke at the deployed data — parses to a real `0.0.0.0`
publish and the suite stays green. That is the precise scenario the new
docstring names as the reason the test was widened. The fix is one line.

> **RESOLVED** in the follow-up commit on `ralph/phase-0`. Both findings, not
> just the blocking one. `docker-compose.yml` is untouched again — the artifact
> has been correct since `e1032a1`; only the guard was not.
>
> Not the suggested one-line comment strip. Two rounds of QA have now been
> spent on spellings a regex over YAML text did not see, and the suggested fix
> closes two of them while leaving a four-space list item —
> `    ports:\n    - "0.0.0.0:8000:8000"`, valid YAML and valid compose —
> still green through the `continue`. `test_no_service_publishes_beyond_loopback`
> now reads `yaml.safe_load(COMPOSE)["services"]` and walks every service the
> parser reports, comparing `str(published)`. That answers finding 2 by
> construction (no hardcoded service tuple) and collapses the whole spelling
> class into one comparison, because the parser sees what Docker sees.
> `pyyaml` added to the **dev** group for it — reason per `AGENTS.md`: it is
> the parser the two prior guards were hand-rolling badly, it is test-only, and
> it is none of the deliberately-excluded dependencies (ORM, Celery, Redis,
> charting). The other four assertions in the file still use the regex
> `service()` helper; they check for the presence of literal text, where it has
> no such hole.
>
> Eight mutations applied to the real `docker-compose.yml`, guard run against
> each, file restored from an in-memory copy:
>
> | Mutation | Result |
> |---|---|
> | none (tree as it stands) | `1 passed` |
> | `- "5432:5432"  # debug` on `db` (**finding 1**) | `1 failed` — `AssertionError: db: 5432:5432` |
> | same, unquoted | `1 failed` — `AssertionError: db: 5432:5432` |
> | `pgadmin` service with `- "5050:80"` (**finding 2**) | `1 failed` — `AssertionError: pgadmin: 5050:80` |
> | four-space list item, `- "0.0.0.0:8000:8000"` | `1 failed` — `AssertionError: web: 0.0.0.0:8000:8000` |
> | `- 8000:8000` (quotes dropped) | `1 failed` — `AssertionError: web: 8000:8000` |
> | `- 8000` (bare container port, unquoted → int) | `1 failed` — `AssertionError: web: 8000` |
> | `- target: 8000` / `published: 8000` | `1 failed` — `AssertionError: web: {'target': 8000, 'published': 8000}` |
> | `ports:` block deleted | `1 failed` — `no published port found — web's ports: block is gone` |
>
> The two the previous guard let through now name the offending service, and
> the long-syntax and 4-space cases report the actual published value instead
> of a misdiagnosis about the guard having stopped reading. Tree clean after
> the run (`git status --porcelain` shows only `pyproject.toml`, `uv.lock`,
> `tests/test_compose.py`); `uv run pytest -q` → `14 passed, 1 warning in
> 1.18s`, same count as before.
>
> No box ticked. `.env.example` — with the `[A-Za-z0-9._~-]`
> `POSTGRES_PASSWORD` constraint — remains the next task.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -q` | `14 passed, 1 warning in 1.03s`. Matches the commit's claim exactly. |
| Diff is only what's claimed | `git show HEAD --stat` | `REVIEWS.md` +34/−2, `tests/test_compose.py` +18/−3. No `TASKS.md`, no `docker-compose.yml`. "No box ticked" verified. |
| **Claim: `- 8000:8000` unquoted → red** | mutation applied to the real file, `pytest -k loopback` | `1 failed` — `AssertionError: web: 8000:8000`. Verified. |
| **Claim: `- "5432:5432"` on `db` → red** | same | `1 failed` — `AssertionError: db: 5432:5432`. Verified — the loop really does visit `db`. |
| **Claim: long syntax → red** | `- target: 8000` / `published: 8000` | `1 failed` — `web: use short syntax with 127.0.0.1`. Verified. |
| **Claim: `ports:` deleted → red** | block removed | `1 failed` — `no published port found`. Verified, the `assert found` backstop works. |
| Single-quoted `'0.0.0.0:8000:8000'` | mutation | `1 failed` — `AssertionError: web: '0.0.0.0:8000:8000'`. Caught. |
| Bare container port `- "8000"` (ephemeral publish on all interfaces) | mutation | `1 failed` — `AssertionError: web: 8000`. Caught. |
| **Quoted port + trailing comment on `db`, `web` left correct** | `- "5432:5432"  # debug` | **`1 passed`** — finding 1 below. |
| Same, unquoted | `- 5432:5432  # debug` | `1 failed` — `AssertionError: db: 5432:5432  # debug`. Only the *quoted* form slips. |
| That the slipping line is a real publish, not a YAML error | `yaml.safe_load` on it | `{'ports': ['5432:5432']}` — a genuine all-interfaces publish of Postgres. |
| Fourth service with a published port | added `pgadmin` with `- "5050:80"` | **`1 passed`** — finding 2 below. |
| YAML-legal 4-space list items under `ports:` | `    - "0.0.0.0:8000:8000"` | `1 failed`, but via the backstop message `no published port found — the guard stopped reading the file`. Red, so not blocking; the message misdiagnoses it. |
| Tree restored after every mutation | `diff` against a pre-run copy, `git status --porcelain` | Identical, clean. `uv run pytest -q` → `14 passed` again. |

Mutations were driven by a throwaway script in `/tmp` that wrote the real
`docker-compose.yml`, ran the guard, and restored the original from an
in-memory copy; `pyyaml` was installed into the venv for the parse check and
uninstalled afterwards (`git status --porcelain` empty at the end).

### Findings

**1. `tests/test_compose.py:59` — a quoted published port with a trailing
comment is skipped silently, so `db` can publish 5432 on `0.0.0.0` and the
suite stays green.** *(blocking)*

The item regex is `^      - \"?([^\"\n]+)\"?$`. Anchored on `$` with `"`
excluded from the character class, it cannot match a line that has anything
after the closing quote. Given:

```yaml
  db:
    ports:
      - "5432:5432"  # debug against the deployed db
```

`re.findall` returns `[]` for `db`, the loop body never runs, and `found` is
still `1` from `web`'s correct line, so the `assert found` backstop does not
fire either. Confirmed: `1 passed, 5 deselected`. `yaml.safe_load` on that
line returns `['5432:5432']`, so Docker publishes Postgres on every interface
of a shared Dokploy host with the credentials from `.env` — the exact outcome
the docstring two lines above says the test was widened to prevent. The
unquoted spelling of the same edit is caught (`AssertionError: db:
5432:5432  # debug`), which makes this inconsistency easy to trust past.

Same defect makes the `web` case report the wrong reason rather than pass:
`- "0.0.0.0:8000:8000"  # debugging` fails with `no published port found — the
guard stopped reading the file`, sending the reader after a broken regex when
the file is fine and the port is open.

*Fix:* strip inline comments before matching, which fixes both spellings and
the 4-space-indent message at once — one line ahead of the existing loop:

```python
items = re.sub(r"\s+#.*$", "", block.group(1), flags=re.M)
for published in re.findall(r"^      - \"?([^\"\n]+)\"?$", items, re.M):
```

**2. `tests/test_compose.py:50` — the service list is hardcoded to
`("web", "db", "cron")`, so a service added later is unguarded.** *(minor)*

Adding `pgadmin` (or `adminer`, or a second `web`) with `ports: - "5050:80"`
passes: `1 passed`. Lower severity than finding 1 — adding a service is a
deliberate multi-line edit, not the one-line debugging edit the test exists to
stop — but the guard advertises "no service publishes beyond loopback" and
does not check that.

*Fix:* derive the names from the file instead of listing them, so new services
are covered by construction:

```python
SERVICES = re.findall(
    r"^  (\w+):$",
    re.search(r"^services:\n(.*?)(?=^\S|\Z)", COMPOSE, re.M | re.S).group(1),
    re.M,
)
```

`test_stack_has_web_db_and_cron` already pins that `web`, `db` and `cron` are
present, so the explicit triple is not load-bearing here.

### Notes — verified, not findings

- The `RESOLVED` block appended to the `e1032a1` review is accurate on every
  line I checked; its four-row mutation table reproduces exactly. It overstates
  only in calling the prior finding closed — three of the four spellings are
  now caught, and the `db`-publishes-5432 scenario it names as "the load-bearing
  one" is still reachable through finding 1.
- `docker-compose.yml` is byte-identical to `e1032a1` (`git show HEAD --stat`),
  as claimed. The artifact remains correct: `127.0.0.1:8000:8000`, `TZ:
  Asia/Kolkata` on `cron`, `tzdata` on the apt line, shared `build: .`, migrate
  before uvicorn — the other five checks in the file all still pass.
- No `TASKS.md` box moved, and none should have: `.env.example` is still open,
  still carrying the `[A-Za-z0-9._~-]` `POSTGRES_PASSWORD` constraint.
- Nothing in this commit touches money, timezone bucketing, `active_transactions`,
  prompts, or `initData` — those decisions are unaffected and their tasks are
  still unchecked.

---

## 2026-08-05 — `e1032a1` — loopback binding, tzdata, DSN password constraint

**Scope:** the three findings from the review of `4341744`. `Dockerfile`,
`docker-compose.yml`, `tests/test_compose.py`, `TASKS.md` (annotation only),
`REVIEWS.md`. No box ticked, no new queue work.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one
finding, and it is not about the artifact. All three fixes are real and correct: the port is loopback, `tzdata`
is on the apt line, the DSN constraint is stated at the interpolation site and
carried onto the `.env.example` task. The commit message's claims all
reproduced, including the mutation results, and I re-derived the anchor merge
independently. The finding is that the **new port guard does not guard** — three
ordinary edits reintroduce the exact 0.0.0.0 exposure it was written to prevent
and it stays green, one of them publishing Postgres. The next task after
`.env.example` is the human Dokploy deploy, which is the same timing argument
the last review made, and the fix is about five lines.

> **RESOLVED** in the follow-up commit on `ralph/phase-0`.
> `test_web_publishes_on_loopback_only` is replaced by
> `test_no_service_publishes_beyond_loopback`, essentially as suggested: it
> reads the list out of a `ports:` block rather than out of the whole service
> block, walks `web`, `db` and `cron` instead of `web` alone, accepts the
> unquoted spelling, rejects long syntax outright, and ends on `assert found`
> so an empty walk is red rather than green. `docker-compose.yml` is unchanged
> — the artifact was already correct, only the guard was not.
>
> Each of the four scenarios re-run against the real file rather than taken
> from the review, then reverted:
>
> | Mutation | Result |
> |---|---|
> | none (tree as it stands) | `14 passed` |
> | `- 8000:8000` (quotes dropped) | `1 failed` — `AssertionError: web: 8000:8000` |
> | `ports: - "5432:5432"` on `db` | `1 failed` — `AssertionError: db: 5432:5432` |
> | `- target: 8000` / `published: 8000` | `1 failed` — `web: use short syntax with 127.0.0.1` |
> | `ports:` block deleted | `1 failed` — `no published port found — the guard stopped reading the file` |
>
> The `db` one naming `db` is the part worth recording: the loop really visits
> every service, so publishing Postgres to debug the deployed stack now fails
> the suite. Tree restored with `git checkout -- docker-compose.yml`;
> `git status --porcelain` clean apart from the test file, `uv run pytest -q`
> → `14 passed, 1 warning in 1.03s`.
>
> Nothing else in this commit: no box ticked, `.env.example` remains the next
> task. The three "Notes — verified facts" above are left alone, including that
> the absence of a `networks:` block is correct.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -q` | `14 passed, 1 warning in 1.13s`. Matches the commit's claim. |
| **Commit's claim: both fixes reverted → 2 red** | reverted `127.0.0.1:` prefix *and* `tzdata` together, `uv run pytest tests/test_compose.py -q` | `2 failed, 4 passed` — exactly `test_image_ships_zone_files` and `test_web_publishes_on_loopback_only`. Claim verified verbatim. |
| Port guard is non-vacuous on the quoted form | port → `- "8000:8000"` alone | `1 failed, 5 passed`, assertion message quoted `'8000:8000'` — it really walked a port list. Verified. |
| tzdata guard is non-vacuous | `cron tzdata` → `cron` alone | `1 failed, 5 passed`, `assert None` at `tests/test_compose.py:39`. Verified. |
| **Commit's claim: the `&app-env` merge survived the new comment** | `uv run --with pyyaml python -c "yaml.safe_load(...)"` | `services: ['cron','db','web']`; `web ports: ['127.0.0.1:8000:8000']`; `web env`: `DATABASE_URL` only; `cron env`: **both** `DATABASE_URL` and `TZ: Asia/Kolkata`. Merge intact, claim verified. |
| **Port guard bypass — unquoted** | port → `- 8000:8000` (parses as the string `'8000:8000'`, a valid mapping — `8000` > 59 so no YAML sexagesimal) | **`6 passed`.** Host-wide exposure restored, guard green. |
| **Port guard bypass — another service** | added `ports: - "5432:5432"` to `db`; parse confirms `db ports: ['5432:5432']` | **`6 passed`.** Postgres on `0.0.0.0:5432`, guard green. |
| **Port guard bypass — long syntax** | `- target: 8000 / published: 8000` (no `host_ip`); parse confirms `[{'target': 8000, 'published': 8000}]` | **`6 passed`.** Guard green. |
| Finding 2's DSN claim, re-run not assumed | `psycopg.conninfo.conninfo_to_dict` over `postgresql://kanakko:<pw>@db:5432/kanakko` | `p@ssw0rd` → **`host='ssw0rd@db'`**, `pw='p'`; `a/b` → `port='a'`, `dbname='b@db:5432/kanakko'`; `pa%ss` and `has space` → `ProgrammingError`; `simple123`, `x+y=z` → correct. The comment on `docker-compose.yml:9-11` and the `[A-Za-z0-9._~-]` constraint on `TASKS.md:26` are both accurate. |
| Task ticks | `git show --stat HEAD`, read `TASKS.md` | No box flipped. `.env.example` line annotated only, still `- [ ]`. `docs/` and `migrations/` untouched. Honest. |
| Secrets | read the diff | No literal token, password, or key. Every value still `${VAR:?see .env.example}`. |
| Spec surface | diff against `docs/DECISIONS.md` | No `float`, no SQL, no category literal, no prompt, no ORM/Celery/Redis/charting dependency, no conversation state. §8 same-image still guarded; §10's `TZ` still present on `cron` only, which §10 wants. |
| Tree restored after every mutation | `git status --porcelain` after each | Empty each time. Final: `14 passed`. |

---

### Finding 1 — the new loopback guard passes on three ordinary ways to reintroduce the exposure, one of them publishing Postgres

`tests/test_compose.py:42-49`

```python
for published in re.findall(r"^\s+- \"([^\"]+)\"$", service("web"), re.M):
    assert published.startswith("127.0.0.1:"), published
```

The guard is right about today's file — I confirmed it goes red on
`- "8000:8000"`, non-vacuously. But it recognises exactly one spelling of a
published port, in exactly one service, and `re.findall` returning nothing is
indistinguishable from a pass. Three edits, all of them things a person writes
without thinking, walk straight past it. Each verified, not argued:

| Edit | What the stack does | Guard |
|---|---|---|
| `- 8000:8000` (quotes dropped) | publishes `0.0.0.0:8000` | `6 passed` |
| `ports: - "5432:5432"` on `db` | publishes `0.0.0.0:5432` | `6 passed` |
| `- target: 8000` / `published: 8000` | publishes `0.0.0.0:8000` | `6 passed` |

The quote-dropping one is the likely one: quoting compose ports is a habit
people have *because* of the `- 22:22` sexagesimal trap, and `8000:8000` is
above 59, so YAML hands back the string either way and nothing complains. The
`db` one is the expensive one. The guard is scoped to `web` because that is
where the last finding was, but the risk is host-wide, and the natural debugging
move — publish 5432 for a minute to run `psql` against the deployed stack — puts
Postgres on a team-owned box's public interface with the credentials from
`.env`, forgets the line, and this suite says the stack is fine. That is
strictly worse than the `/telegram/webhook` exposure the guard was written for.

Failure scenario, concretely: Phase 3 or a debugging pass adds
`ports: - 5432:5432` under `db`. `uv run pytest` → `14 passed`. The file is
pasted into Dokploy on `doc-panel` (49.12.44.133). Postgres answers on
`0.0.0.0:5432` behind a Hetzner firewall whose documented posture is 80/443
public and SSH restricted — so possibly closed at the edge, but the host's other
tenants are inside it. Nothing errors, and the one file that is supposed to
notice is green.

Fix — read the ports out of `ports:` rather than out of the whole block, accept
both spellings, cover every service, and assert the walk was non-empty so a
future syntax change fails loudly instead of silently. No new dependency:

```python
def test_no_service_publishes_beyond_loopback():
    """This file is pasted into Dokploy on a shared host (DECISIONS §14).

    Any 0.0.0.0 binding puts the service on a team-owned box's public
    interface. Nothing about it looks wrong, so it has to be asserted.
    """
    found = 0
    for name in ("web", "db", "cron"):
        block = re.search(r"^    ports:\n((?:      [-\s].*\n)+)", service(name), re.M)
        if not block:
            continue
        for published in re.findall(r"^      - \"?([^\"\n]+)\"?$", block.group(1), re.M):
            found += 1
            assert published.startswith("127.0.0.1:"), f"{name}: {published}"
        # long syntax has no bare list items — fail rather than skip it
        assert "target:" not in block.group(1), f"{name}: use short syntax with 127.0.0.1"
    assert found, "no published port found — the guard stopped reading the file"
```

The `assert found` is the part that matters most: it is what turns the next
silent bypass into a red test.

I ran that replacement against all five files rather than proposing it untested
(`/tmp/check_suggestion.py`, not committed):

```
  PASS  current file (must PASS)
  FAIL  unquoted 8000:8000        -> web: 8000:8000
  FAIL  db publishes 5432         -> db: 5432:5432
  FAIL  long syntax no host_ip    -> web: target: 8000
  FAIL  ports block deleted       -> no published port found — the guard stopped reading the file
```

Green on the tree as it stands, red on every bypass above and on the guard
losing its footing. Take it or improve on it — the requirement is that all four
of those go red, not this particular regex.

---

### Notes — verified facts, not findings, so nobody re-opens them

- **`python:3.12-slim` already ships `tzdata`.** The last review left this
  `UNVERIFIED` and this commit says it "no longer matters either way". It does
  resolve, and in the reassuring direction: `docker-library/python`'s
  `3.12/slim-bookworm/Dockerfile` and `3.12/slim-trixie/Dockerfile` both run
  `apt-get install -y --no-install-recommends ca-certificates netbase tzdata` in
  the base layer. So `Dockerfile:5-7`'s "tzdata is not optional" is belt-and-
  braces against the base image changing, not the repair of a live defect — keep
  it, the reasoning is sound, but the sidecar was never on UTC for this reason.
  It also means there is no `DEBIAN_FRONTEND` hazard here: `tzdata` is already at
  the newest version, so the apt call cannot reach the debconf geographic-area
  prompt that hangs builds.
- **The commit's load-bearing Traefik claim holds.** Dokploy's Docker Compose
  domain docs: *"At runtime, during the deployment phase, Dokploy automatically
  adds Traefik labels internally to your Docker Compose file"* and attaches the
  service to `dokploy-network`; the manual route is to declare
  `networks: dokploy-network: external: true` yourself. `DEPLOYMENT.md` step 4
  uses `domain.create`, i.e. the automatic path. So Traefik really does reach the
  container over the docker network and never over the published port — the
  loopback binding is safe, and the **absence of a `networks:` block in
  `docker-compose.yml` is correct, not an oversight.** Don't "fix" it.
- The Phase 3 notes from the last review all still stand and are all still
  unchecked work: cron not passing its environment to spawned jobs, `PATH` not
  reaching them, the `db` container running UTC, and whether the Debian `cron`
  daemon honours `TZ` at all versus `CRON_TZ=`. `docker build` is still refused
  here, so that last one stays `UNVERIFIED`.

---

## 2026-08-05 — `4341744` — compose stack (web, db, cron) + Dockerfile

**Scope:** Phase 0 task 5. New `Dockerfile`, `.dockerignore`,
`docker-compose.yml`, `tests/test_compose.py`; `TASKS.md` line 25 ticked.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — three
findings, none of them a stub and none
of them the tick being false. The task is genuinely done: three services exist,
`cron` really shares the image and really carries `TZ=Asia/Kolkata`, and I
reproduced every mutation the commit message claims to have run. The findings
are about the *deployed artifact*, and their timing is why they block: the next
two queue items are `.env.example` (finding 2 lives there) and the human Dokploy
deploy (finding 1's blast radius). All three fixes together are about four
lines, and they get an order of magnitude more expensive after a human has
deployed this file.

The commit message is unusually honest — it flags the unbuilt image itself. I
confirmed that block independently rather than taking its word for it.

> **RESOLVED** in the follow-up commit on `ralph/phase-0`. All three findings
> fixed as suggested.
>
> Finding 1: `docker-compose.yml:41` is now `"127.0.0.1:8000:8000"`. Finding 3:
> `Dockerfile:7` installs `cron tzdata` — same layer, same apt call, so the zone
> files stop depending on what `python:3.12-slim` happens to ship. Finding 2:
> the constraint is stated at the interpolation site (`docker-compose.yml:8-11`)
> and carried onto the `.env.example` queue item in `TASKS.md`, which is the
> next task and where the value is actually chosen — nothing else in this commit
> touches the queue.
>
> Two new checks, both for the findings whose absence is silent (finding 2 fails
> loudly in a crash loop, so it gets a comment, not an assertion):
> `test_web_publishes_on_loopback_only` walks every published port in `web` and
> requires a `127.0.0.1:` prefix; `test_image_ships_zone_files` requires `tzdata`
> on the `apt-get install` line. Confirmed both catch the defect rather than
> assumed: with both fixes reverted, `uv run pytest tests/test_compose.py -q`
> reported `2 failed, 4 passed` and the failures were exactly those two — the
> port one non-vacuously, its assertion message quoting the `'8000:8000'` it
> actually read, so it is reading the port list and not passing on an empty
> `findall`. Restored: `14 passed, 1 warning in 1.10s`.
>
> Re-parsed the file after editing inside the `&app-env` anchor, since a comment
> there could have broken the merge: `services: ['cron','db','web']`,
> `web ports: ['127.0.0.1:8000:8000']`, `web env keys: ['DATABASE_URL']`,
> `cron env` still carries **both** `DATABASE_URL` and `TZ: Asia/Kolkata`. The
> merge is intact.
>
> Still `UNVERIFIED` and correctly left to Phase 3, as the finding directs:
> whether the Debian `cron` daemon honours `TZ` at all versus `/etc/localtime`
> or a `CRON_TZ=` line. `docker build` is still refused here, so that gets
> confirmed against a built image on the crontab task, which should open the
> file with `CRON_TZ=Asia/Kolkata`. The finding's other half — whether the base
> image ships `Asia/Kolkata` — no longer matters either way now that `tzdata` is
> installed explicitly.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -q` | `12 passed, 1 warning in 1.08s`. Matches the commit's claim. |
| **Docker really is blocked here** | `docker ps` · `docker images` · `docker info --format '{{.ServerVersion}}'` · `docker compose -f docker-compose.yml config -q` · `docker pull python:3.12-slim` | The daemon **is** up and reachable (`docker ps` returned a running `mongo:6.0`), but every write/inspect verb is refused: `This command requires approval`. So the commit's "docker is UNKNOWN on this host" is understated in one direction and correct in the other — docker exists, and `compose config` is still not runnable. **I could not build the image either.** |
| Compose file is valid YAML and the anchor merges | `uv run --with pyyaml --no-project python -c "yaml.safe_load(...)"` from `/tmp` (no change to `pyproject.toml` or `uv.lock`) | `services: ['cron','db','web']`; `web` → `build=.`, env has `DATABASE_URL` only, `ports=['8000:8000']`; `cron` → `build=.`, env has **both** `DATABASE_URL` and `TZ: Asia/Kolkata` (the `<<: *app-env` merge works); `db` → no `build`, no `ports`; `volumes: {'pgdata': None}`. |
| **Commit's claim: TZ removed/UTC → red** | edited `docker-compose.yml:49` to `TZ: UTC`, `uv run pytest tests/test_compose.py -q` | `1 failed, 3 passed` — `test_cron_runs_in_asia_kolkata`, `assert None` at `tests/test_compose.py:27`. Verified. Restored with `git checkout --`, tree clean. |
| **Commit's claim: migrate dropped from web's command → red** | replaced `web.command` with a bare `uvicorn …`, same run | `1 failed, 3 passed` — `test_web_migrates_before_it_serves` at `tests/test_compose.py:44`. Verified. Restored, clean. |
| **Commit's claim: cron given its own image → red** | `cron.build: .` → `image: kanakko-cron:latest`, same run | `1 failed, 3 passed` — `test_web_and_cron_share_one_image` at `tests/test_compose.py:37`. Verified. Restored, clean. |
| **The load-bearing packaging claim: `MIGRATIONS` resolves via the install, not `cwd`** | `ls .venv/lib/python3.12/site-packages \| grep pth` → `_editable_impl_kanakko.pth`; `cat` it → `/home/joshy/jk_projects/Kanakko`; then `cd /tmp && uv run --project … python` printing `kanakko.__file__` and `migrate.MIGRATIONS` | `cwd /tmp` · `file …/Kanakko/kanakko/__init__.py` · `MIGRATIONS …/Kanakko/migrations True`. The install really is editable and the path really comes from the package location, not the working directory. In the image that maps to `/app/kanakko` → `/app/migrations`, which `Dockerfile:15` COPYs. The claim holds. |
| Nothing in the build needs a file the Dockerfile doesn't COPY | read `pyproject.toml` | No `readme =` key, `[tool.hatch.build.targets.wheel] packages = ["kanakko"]`. `pyproject.toml`, `uv.lock`, `kanakko/` are all COPYed before `uv sync`, so the hatchling build has what it needs. No missing-`README.md` build break. |
| `postgres:16-alpine` can run the schema | read `migrations/001_init.sql` | `GENERATED ALWAYS AS IDENTITY` (PG10+), `NUMERIC(12,2)`, `TIMESTAMPTZ`, one view. No extension, no `CREATE EXTENSION`, nothing that needs the non-alpine image. Fine on 16. |
| Secrets | read `Dockerfile`, `docker-compose.yml`, `tests/test_compose.py`, `.dockerignore` | No literal password, token, or key. Every value is `${VAR:?see .env.example}`. `.dockerignore` excludes `.env`; `.gitignore` already had `.env` and `*.env`. `db` publishes **no** port. Clean. |
| Spec surface | read the diff against `docs/DECISIONS.md` | No `float`, no SQL, no category literal, no LLM prompt, no ORM/Celery/Redis/charting dependency, no conversation state. §8's "same image, different command" is implemented and now guarded. §9/§3/§6/§11 have no new surface in this commit. |
| Task tick | `git show --stat HEAD` | Only `TASKS.md:25` flipped. `docs/` and `migrations/` untouched — no applied migration edited, no spec edited to match the code. |

**On the tick:** honest. The task asked for `web`, `db`, `cron`, same image,
cron command, `TZ=Asia/Kolkata`. All present, all real, no stub. Finding 3 is
about whether that TZ *takes effect*, not about whether it was written.

---

### Finding 1 — `web` publishes port 8000 on every host interface (blocking)

`docker-compose.yml:37-38`

```yaml
    ports:
      - "8000:8000"
```

The `ponytail:` comment above it is right that Traefik reaches the container
over the compose network — which is exactly why the published port is not
needed in production. But the same file is what gets pasted into Dokploy, so
the binding ships: `0.0.0.0:8000` on a **team-owned** host (DECISIONS §14 names
that explicitly).

Failure scenario: the human deploy task lands this file on `doc-panel`. Today
the only route is `/healthz`, so the damage is a squatted host port on shared
infra. One phase later, `POST http://<host-ip>:8000/telegram/webhook` reaches
the update handler in plaintext, from anywhere, **bypassing the HTTPS path
DECISIONS §14 chose the webhook for** and bypassing anything the domain gets in
front of it. Phase 4's Mini App routes inherit the same door. Nothing errors —
the app answers normally, just on a channel nobody meant to open.

Fix — one line, and local `docker compose up` behaves identically
(`http://localhost:8000/healthz` still works), because Traefik never used the
published port:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

### Finding 2 — `DATABASE_URL` is string-interpolated, so a password with `@`, `/`, `%` or a space silently builds the wrong DSN (blocking)

`docker-compose.yml:9`

```yaml
  DATABASE_URL: "postgresql://${POSTGRES_USER:?…}:${POSTGRES_PASSWORD:?…}@db:5432/${POSTGRES_DB:?…}"
```

Composing one credential set instead of a fourth secret is the right call. The
gap is that the password goes into a URI without percent-encoding. I ran the
project's own parser (`psycopg.conninfo.conninfo_to_dict`) over
`postgresql://kanakko:<pw>@db:5432/kanakko`:

| `POSTGRES_PASSWORD` | parsed as |
|---|---|
| `simple123` | `user=kanakko password=simple123 host=db port=5432 dbname=kanakko` ✅ |
| `p@ssw0rd` | `password='p'`, **`host='ssw0rd@db'`** |
| `a/b` | `user=None password=None`, **`port='a'`, `dbname='b@db:5432/kanakko'`** |
| `pa%ss` | `ProgrammingError: invalid percent-encoded token: "pa%ss"` |
| `has space` | `ProgrammingError: unexpected spaces found in "has space"` |
| `x+y=z`, `k#1` | parsed correctly ✅ |

Failure scenario: the operator pastes a generated password containing `@` into
Dokploy. `db` receives it raw as `POSTGRES_USER`/`POSTGRES_PASSWORD` and comes
up **healthy**, so `depends_on: service_healthy` is satisfied; `web` then dies
in `python -m kanakko.migrate` on a host named `ssw0rd@db`. It fails loudly —
but at the wrong thing. The error names DNS, the database is demonstrably fine,
and the actual cause is one character in a secret that can't be read from the
logs. `restart: unless-stopped` turns it into a crash loop.

Fix — cheapest thing consistent with the one-credential-set decision, and
`.env.example` is literally the next task: state the constraint where the value
is chosen, plus a matching comment on line 9.

```
# Must be URL-safe — it is interpolated into DATABASE_URL unencoded.
# Use [A-Za-z0-9._~-] only; @ / % and spaces break the DSN.
POSTGRES_PASSWORD=
```

### Finding 3 — the TZ guard asserts the string, not the effect; `TZ` is a silent no-op if the image can't resolve the zone (blocking)

`tests/test_compose.py:26-27`, `Dockerfile:6-8`

`test_cron_runs_in_asia_kolkata` greps `TZ: Asia/Kolkata` out of the YAML. That
catches the value being deleted or changed — I confirmed both. It cannot catch
the value being *ignored*, which is the same 21:00→02:30 outcome the test's own
docstring is written to prevent.

glibc's fallback is silent. Measured on this host:

```
$ TZ=Asia/Kolkata date   → Wed Aug  5 06:15:16 PM IST 2026
$ TZ=Asia/Nowhere  date  → Wed Aug  5 12:45:16 PM Asia 2026
$ TZ=UTC           date  → Wed Aug  5 12:45:16 PM UTC 2026
```

An unresolvable zone name produces UTC wall-clock, exit 0, no warning — exactly
the 5:30 shift DECISIONS §10 calls out, and `Asia/Nowhere` even reports itself
as `Asia`.

Two things that decide whether the setting bites, and I could verify **neither**
here — `docker pull python:3.12-slim` and web access were both refused, so both
are `UNVERIFIED`, not "broken":

1. Whether `python:3.12-slim` ships `/usr/share/zoneinfo/Asia/Kolkata`. If it
   doesn't, `TZ: Asia/Kolkata` is a no-op and the sidecar is on UTC with all
   four guards green.
2. Whether the Debian `cron` **daemon** honours the `TZ` environment variable at
   all, as opposed to reading `/etc/localtime` or a `CRON_TZ=` line in the
   crontab.

Neither is worth another round of guessing, because one word closes the first
and one line closes the second. `Dockerfile:6-8` already runs apt:

```dockerfile
    && apt-get install -y --no-install-recommends cron tzdata \
```

— no extra layer, and it stops the guarantee depending on what the base image
happens to include. Then in the Phase 3 crontab task, open the file with
`CRON_TZ=Asia/Kolkata` so the schedule states its own zone rather than
inheriting one. Whoever picks up Phase 3 should confirm (1) and (2) against a
built image and record the observed output, since by then `docker build` is
reachable.

---

### Notes for Phase 3 — not findings, don't fix them now

The crontab task is unchecked, so none of this counts against this commit. It
is written down because the compose file makes two of them *look* handled.

- **Cron does not hand its own environment to the jobs it spawns.** `cron -f`
  has `DATABASE_URL` in its env because `docker-compose.yml:45` merges it in,
  but a spawned job gets cron's minimal environment. `kanakko.jobs.*` will see
  no `DATABASE_URL` and `migrate.main()`-style code will exit
  `DATABASE_URL is not set`. Same for `PATH`: `Dockerfile:22` puts
  `/app/.venv/bin` on the image `PATH`, and cron jobs won't have it, so a bare
  `python` in a crontab line is the system interpreter without the deps.
- **The `db` container runs UTC.** `AT TIME ZONE 'Asia/Kolkata'` is explicit and
  unaffected, but a report query that reaches for `CURRENT_DATE` or `now()::date`
  buckets on the UTC date and is wrong for 5.5 hours a day. Use
  `(now() AT TIME ZONE 'Asia/Kolkata')::date`.
- **`web` deliberately has no `TZ`, and that is correct** — leave it. DECISIONS
  §10 wants the zone stated explicitly at each call site; a container-wide `TZ`
  on `web` would paper over a naive `datetime.now()` instead of exposing it.

**Tree state after this review:** `git status --porcelain` clean. Every mutation
above was reverted with `git checkout -- docker-compose.yml` and `uv run pytest
-q` returns `12 passed` on the restored tree. No image was built, no container
started, no `pyproject.toml`/`uv.lock` change (the pyyaml parse ran through
`uv run --with pyyaml --no-project`).

---

## 2026-08-05 — `d7a97fd` — migrator refuses to succeed with no migrations

**Scope:** fixes both open findings on `3f33abf` — adds an up-front empty-glob
guard to `migrate()` (`kanakko/migrate.py:28-33`) and deletes the
`TEST_DATABASE_URL` branch from the test fixture, plus a new check
`test_migrate_refuses_to_succeed_with_no_migrations`. `TASKS.md` untouched, no
queue movement.

**Status: ✅ DONE** — no blocking issues. Both findings are genuinely fixed, and
I reproduced the *original* defect scenario end to end rather than trusting the
commit message: an unpacked wheel with no `migrations/`, pointed at a live
throwaway cluster from `cwd=/tmp`, previously exited **0** printing `nothing to
apply`; it now exits **1** with `RuntimeError: no migrations found at
/tmp/qa_e2e3/site/migrations` and leaves no `schema_migrations` table behind.
The guard is also precise — the same layout *with* `migrations/` present applies
and then no-ops, exit 0 both runs. Two non-blocking findings recorded below; the
compose task is not blocked on either.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -v` | `8 passed, 1 warning in 1.02s`. The two Postgres-backed tests **PASSED**, not skipped — the server path really ran on this machine. |
| **Commit's claim: the new check catches the defect** | deleted the 6-line guard block from `kanakko/migrate.py`, `uv run pytest -q` | `1 failed, 7 passed` — the failure is exactly `test_migrate_refuses_to_succeed_with_no_migrations`. Note the *mode*: `AttributeError: 'NoneType' object has no attribute 'transaction'` at `kanakko/migrate.py:30`, not a missing-raise. That still fails the `pytest.raises(RuntimeError)`, and because the test passes `conn=None` it also pins the guard's *position* — moving it below `with conn.transaction()` would fail the same way. Claim verified. |
| Tree restored after that mutation | `cp` back, `uv run pytest -q`, `git status --short` | `8 passed`, clean. |
| **Commit's claim: the new check needs no server** | `uv run pytest tests/test_migrate.py::test_migrate_refuses_to_succeed_with_no_migrations -v` | `1 passed in 0.08s` vs `1.02s` for the full suite — no cluster booted. It does not take the `conn` fixture, so it cannot be skipped on a Postgres-less machine. Verified. |
| Wheel contents | `uv build --wheel -o /tmp/qa_wheel`, list the zip via `zipfile` | `kanakko/__init__.py`, `kanakko/app.py`, `kanakko/migrate.py`, dist-info. **Still no `migrations/`** — deferred to the compose task, as the finding suggested. |
| **The finding-1 scenario, end to end** | unpacked that wheel to `/tmp/qa_e2e3/site`, `PYTHONPATH` so only the wheel's `kanakko` imports, real throwaway cluster, `python -m kanakko.migrate` from `cwd=/tmp` | `kanakko : /tmp/qa_e2e3/site/kanakko/__init__.py`, `MIGRATIONS: /tmp/qa_e2e3/site/migrations \| exists: False`, then **`EXIT CODE: 1`**, `stdout: ''`, `RuntimeError: no migrations found at /tmp/qa_e2e3/site/migrations`. Schema after: `(transactions, schema_migrations) = (None, None)` — the guard fires before the bookkeeping table is created, so it leaves nothing behind. Observed, not inferred. |
| **The guard does not false-positive** | same layout, `migrations/` copied next to the package, migrator run twice | `run 1: exit=0 stdout='applied 001_init.sql'` · `run 2: exit=0 stdout='nothing to apply'` · `objects = ('transactions', 'active_transactions', 'schema_migrations')` · `recorded = [('001_init.sql',)]`. Either fix the compose task picks (`force-include` or `COPY`) will work; only the broken packaging crashes. |
| **What the suite reports with no server binaries** | pytest plugin stubbing `pg_bin` to `None`, `uv run pytest -p qa_noserver -q -rs` | `6 passed, 2 skipped`, **exit code 0**. The skipped pair is `test_migrations_apply_and_are_recorded` and `test_schema_stores_money_exactly`. See finding 1. Scratch plugin removed; `git status --short` clean. |
| Docstring's stated reason is factually true | `assert migrate(conn) == []` on the second call, plus `run 2` above | A persistent DSN really would pass once and fail after. The claim in `tests/test_migrate.py:5-7` holds. |
| Task ticks | `git diff HEAD~1 HEAD --stat -- TASKS.md AGENTS.md docs/ migrations/` | empty. Nothing ticked, no spec edited, no applied migration touched. Consistent with the commit message. |
| Secrets | `grep -cE "password\|secret\|token\|api[_-]?key\|float"` over the diff | `0`. No credential, no `float`. |
| Tree and processes restored | `git status --short`, `pgrep -af "postgres.*qa_e2e"` | clean, `(none)`. Scratch clusters stopped, `/tmp/qa_*` removed. |

**Spec conformance:** this commit touches packaging and a test fixture. It adds
no dependency, no ORM, no float, no SQL, no timestamp handling, no category
literal and no LLM prompt, so §9/§10/§11/§3/§6 have no new surface. §7's "plain
SQL, numbered `.sql`" is untouched. The one spec-adjacent effect is positive:
the §9 money round-trip (`test_schema_stores_money_exactly`) can no longer pass
vacuously against a schema that was never created, because the migrator now
refuses to report success on an empty set.

**Not a stub:** the guard is four real lines with a real failure path, and I
executed that path from an installed artifact against a live server, not from
the test suite.

### Findings

**1 — Low/medium, non-blocking. With `TEST_DATABASE_URL` gone there is now no
way to run the two server-backed tests on a machine without `initdb`/`pg_ctl`,
and their absence is a green build.** `tests/test_migrate.py:28-30`

Removing the branch was one of the two fixes the prior review offered, so this
is not a wrong call. But the docstring's justification is narrower than the case
it removes. `tests/test_migrate.py:5-7` argues that a persistent DSN "would pass
once and then fail on every later run" — true for a developer's own database,
and I confirmed the underlying property. It is not true for the case such an env
var normally exists to serve: an ephemeral CI service container, which is
*virgin on every run* and where the assertion would hold every time.

*Failure scenario:* CI (there is no `.github/` yet, so this is the moment before
it exists) runs the suite in a python image with `psycopg` but no Postgres
*server* binaries — the normal shape, since the server lives in a `db` service
container. Observed with `pg_bin` stubbed to `None`: `6 passed, 2 skipped`,
**exit code 0**. The two that vanish are the only checks that execute DDL, and
one of them, `test_schema_stores_money_exactly:76`, is the sole server-enforced
guarantee that `amount` is really `NUMERIC(12,2)` and that
`active_transactions` really hides soft-deleted rows. `AGENTS.md:37` calls money
the one area where "it's probably fine" is not acceptable; here it goes missing
without turning the build red.

*Why not blocking:* nothing consumes this today — no CI exists, and on this
machine and any Debian box with `postgresql-16` installed the tests run for
real. It costs nothing to leave until CI or a container test run appears.

*Suggested fix (whenever CI lands, not now):* rather than restoring the env var
as it was, make the skip loud in the environment that matters — e.g. honour
`TEST_DATABASE_URL` again but have that branch `CREATE DATABASE` a
uniquely-named scratch DB and drop it (the prior review's second option, which
survives repeated runs), or set a `--strict-markers`-style opt-in such as
`KANAKKO_REQUIRE_PG=1` that turns the skip into a failure. Either way the rule
worth encoding is: a run that silently omits the money round-trip should not
exit 0.

**2 — Low. The new check exercises an empty directory; production's failure is a
missing one.** `tests/test_migrate.py:55`

`monkeypatch.setattr(..., tmp_path)` points `MIGRATIONS` at a directory that
exists and is empty. The scenario the guard is written for — the docstring at
`:49-50` says so — is `site-packages/migrations`, which does not exist at all.
Those are the same code path only because `Path.glob` on a missing directory
returns empty rather than raising.

*Failure scenario:* it is thin, which is why this is Low — if a future Python or
a `Path` subclass made `glob` raise `FileNotFoundError` on a missing directory,
the guard's `RuntimeError` would never be reached, the test would still pass on
its empty-but-present directory, and the deploy would fail with a different
error than the one the guard was written to produce. I checked the real case
rather than assuming it: the e2e run above hit a genuinely absent
`/tmp/qa_e2e3/site/migrations` and did raise the `RuntimeError`, so the guard is
correct today.

*Suggested fix:* one word — `tmp_path / "absent"` instead of `tmp_path`, which
tests the shape production actually has. The empty-directory case is then also
still covered, since both reach the same `if not files`.

### Noted, not findings

- **The wheel still ships no `migrations/`** (verified above). The commit defers
  the `force-include`-vs-`COPY` choice to the compose task and the prior review
  suggested exactly that. The deferral is now safe rather than silent, and I
  confirmed both halves: broken packaging → exit 1, correct packaging → applies
  cleanly. Worth carrying into the compose task as a checklist item, not a
  finding here.
- The skip message at `tests/test_migrate.py:30` names only
  `/usr/lib/postgresql`, but `pg_bin` also searches `/usr/local/pgsql/bin`. Only
  matters to whoever reads that message while debugging a skip.
- The one-transaction property is still unpinned by any test — carried forward
  from the `3f33abf` review, unchanged by this commit and not made worse by it.

---

## 2026-08-05 — `3f33abf` — migration runner + `001_init.sql` executed on Postgres

**Scope:** adds `kanakko/migrate.py` (applies `migrations/*.sql` in filename
order in one transaction, records each in `schema_migrations`, advisory-locked)
and `tests/test_migrate.py` (boots a throwaway PG cluster, applies the
migration, round-trips money through it); ticks Phase 0 task 4 in `TASKS.md` and
swaps the `psql -f` line in `AGENTS.md` for the runner.

**Status: ⚠️ CHANGES REQUESTED** — the runner is correct, the tick is honest,
and every claim in the commit message that I re-tested held up. One open
finding, and it is worth fixing *before* the next task rather than after: the
runner exits 0 reporting success when it finds no migration files, and the
built wheel does not contain `migrations/`. The next task is the compose file
that runs this on web start.

> **RESOLVED** in the follow-up commit on `ralph/phase-0`. Finding 1 is fixed by
> the suggested guard: `migrate()` now globs once, up front, and raises
> `RuntimeError(f"no migrations found at {MIGRATIONS}")` on an empty set, so an
> empty `migrations/` is a startup crash naming the path it searched instead of
> `exit 0, nothing to apply`. Whether the wheel should also carry `migrations/`
> is left to the compose task, as the finding suggests — the guard is what makes
> either choice fail loudly. Finding 2 is fixed by deleting the
> `TEST_DATABASE_URL` branch; the throwaway cluster is the path that actually
> runs, and the docstring now records *why* there is no DSN switch instead of
> advertising one that does not survive its second use.
>
> New check: `test_migrate_refuses_to_succeed_with_no_migrations` points
> `MIGRATIONS` at an empty `tmp_path` and expects the raise. It needs no server
> — the guard runs before `conn` is touched — so it cannot be silently skipped
> on a machine without Postgres. Confirmed it catches the defect: with the guard
> absent from `kanakko/migrate.py`, `uv run pytest -q` reported `1 failed, 7
> passed` and the failure was exactly that test; with it restored, `8 passed, 1
> warning in 1.10s`.

### What I actually checked

| Check | Command | Result |
|---|---|---|
| Suite passes | `uv run pytest -v` | `7 passed, 1 warning in 1.00s`. The two new tests **PASSED**, not skipped — so the Postgres path really ran here. |
| A real server really boots | `uv run pytest tests/test_migrate.py -v -s` | `initdb` output, then `LOG: starting PostgreSQL 16.14 (Ubuntu 16.14-0ubuntu0.24.04.1)`, `listening on Unix socket "/tmp/pytest-of-joshy/pytest-8/pgsock0/.s.PGSQL.5432"`. No TCP line — the `-h ''` claim is true, a developer's own Postgres cannot be hit. |
| Not silently skipping | `pg_bin('initdb')` / `pg_bin('pg_ctl')` / `TEST_DATABASE_URL` via python | `/usr/lib/postgresql/16/bin/initdb`, `/usr/lib/postgresql/16/bin/pg_ctl`, `None`. Binaries present, no env override — the skip branch was not taken. |
| **Applied schema, read from the server** (not from the `.sql` text) | own throwaway cluster + `information_schema.columns` | `('transactions','amount','numeric',12,2,'NO')` · `('transactions','category','text',None,None,'YES')` · every `*_at` column `timestamp with time zone`. §9, §3 and §10 enforced by a server, not by a regex. |
| View definition, as the server stored it | `pg_get_viewdef('active_transactions', true)` | `SELECT txn_id, … FROM transactions WHERE deleted_at IS NULL;` ✓ §6 |
| `NUMERIC(12,2)` actually rounds | `INSERT … amount = Decimal("10.005")` | stored as `Decimal('10.01')`; `SELECT sum(amount), pg_typeof(sum(amount))` → `(Decimal('10.01'), 'numeric')`. The sum stays `numeric` → `Decimal`, no float in the middle. |
| **Commit's claim: a syntax error rolls the whole run back** | scratch `999_bad.sql` with `PRIMRY KEY`, then `to_regclass` on a fresh connection | `SyntaxError: syntax error at or near "PRIMRY"` — the *real* error, not "current transaction is aborted" — and `(None, None)`: neither `users` nor `schema_migrations` survived. Verified, not assumed. Scratch file removed. |
| **Commit's claim: the advisory lock prevents a boot collision** | two threads, two connections, `migrate()` concurrently on a virgin DB | `{0: ['001_init.sql'], 1: []}`. One applies, the other no-ops. No duplicate-key error. Verified. |
| **Commit's claim: the test catches an unrecorded migration** | deleted the `INSERT INTO schema_migrations` line, `uv run pytest tests/test_migrate.py -q` | `2 failed` — `test_migrations_apply_and_are_recorded` and `test_schema_stores_money_exactly`. Restored; `git status --short` clean. Verified. |
| **Silent-success path** | `MIGRATIONS` repointed at a nonexistent dir, `migrate(conn)` | returned `[]` — no error. See finding 1. |
| **Wheel contents** | `uv build --wheel`, list the zip | `kanakko/__init__.py`, `kanakko/app.py`, `kanakko/migrate.py`, dist-info. **No `migrations/`.** See finding 1. |
| **Silent-success end to end** | installed that wheel non-editable into a scratch 3.12 venv, `DATABASE_URL=… python -m kanakko.migrate` against a fresh cluster, `cwd=/tmp` | **`exit code: 0`, `stdout: nothing to apply`**, empty schema. Observed, not inferred. See finding 1. |
| Local install is editable (so this does not bite today) | `uv run python -c "import kanakko; print(kanakko.__file__)"` | `/home/joshy/jk_projects/Kanakko/kanakko/__init__.py` — `_editable_impl_kanakko.pth`. The repo path resolves correctly right now. |
| Secrets | `grep -cniE "password\|secret\|token\|api[_-]?key"` on both new files | `0` and `0`. `--auth=trust` in the test is a throwaway cluster on a tmp-dir unix socket with TCP disabled — not a credential. |
| Tree restored | `git status --short`, `git diff --stat` | clean, empty. No review artifacts, no stray postmasters (`pgrep -af "postgres.*kanakko_probe"` → none). |

**Spec conformance:** §7 plain SQL, `psycopg`, numbered `.sql` — no ORM, no
Alembic, no new dependency. §9/§3/§10/§6 are now *server-enforced*, per the
table above. Nothing in this commit touches an LLM prompt, a category, or a
day/month boundary, so §10 bucketing and §11 are not yet in play.

**On the tick in `TASKS.md`:** legitimate, and it discharges finding 5 on
`4b92212` for real. That finding was "this DDL has never been parsed by a
server." It has now been parsed and executed by PostgreSQL 16.14, and a
permanent test keeps it that way rather than relying on a one-off command
someone once ran. `GENERATED ALWAYS AS IDENTITY` and the partial index both
survive a real parser. Not a stub: the runner orders, records, skips,
transacts and locks, and I exercised each of those.

### Findings

**1. `kanakko/migrate.py:12` — the runner reports success when it finds no
migrations, and the wheel does not ship them.** *(open, blocking the next task)*

`MIGRATIONS = Path(__file__).parent.parent / "migrations"` resolves relative to
the *installed* module. Editable installs put that at the repo root, which is
why everything works today. `[tool.hatch.build.targets.wheel] packages =
["kanakko"]` in `pyproject.toml:24-25` means a built wheel carries no
`migrations/` directory — confirmed by listing the zip. `MIGRATIONS.glob("*.sql")`
then yields nothing, the loop body never runs, `migrate()` returns `[]`, and
`main()` prints `nothing to apply` and exits **0**.

Failure scenario, observed rather than reasoned: install the wheel
non-editable, point `DATABASE_URL` at an empty database, run `python -m
kanakko.migrate` → `exit code: 0`, `stdout: nothing to apply`, and the database
still has no tables. In the compose stack the commit message describes next —
`web` running the migrator before uvicorn — that is a green deploy on an empty
schema. The first webhook then dies on `relation "transactions" does not exist`,
far from the cause. "Applied nothing" and "nothing left to apply" are currently
the same output, and only one of them is good news.

Suggested fix: refuse to succeed on an empty set. In `migrate()`, before the
loop:

```python
files = sorted(MIGRATIONS.glob("*.sql"))
if not files:
    raise RuntimeError(f"no migrations found at {MIGRATIONS}")
```

That one guard converts a green deploy on an empty schema into a startup crash
naming the path it looked in. Whether `migrations/` should also ship inside the
wheel (`[tool.hatch.build.targets.wheel.force-include]`) or be `COPY`ed by the
Dockerfile is a call best made with the compose file in hand — the guard is
what stops either choice from failing quietly.

**2. `tests/test_migrate.py:22` — the `$TEST_DATABASE_URL` escape hatch works
exactly once per database.** *(open, minor)*

The module docstring offers `TEST_DATABASE_URL` as a supported alternative to
the throwaway cluster. But `test_migrations_apply_and_are_recorded:60` asserts
`migrate(conn) == names`, which only holds against a *virgin* database. Point it
at any database the suite has already run against and the first assertion fails
with `[] != ['001_init.sql']` — the tests are not usable twice on a persistent
DSN. This fails loudly, so nothing silent rides on it; it is listed because the
docstring advertises a path that does not survive its second use.

Worth noting alongside it: `migrate()` **commits**, so running the suite against
a real DSN applies the migrations there for keeps. The test's own rows are
rolled back at `tests/test_migrate.py:89`; the schema change is not.

Suggested fix: either drop the `TEST_DATABASE_URL` branch (the throwaway cluster
is the path that actually runs), or have that branch create and drop a
uniquely-named scratch database.

### Notes, not findings

- Nothing pins the one-transaction property. Both tests still pass if
  `conn.transaction()` at `kanakko/migrate.py:31` were replaced by a commit per
  file. I confirmed the property holds today by hand (the `PRIMRY KEY` run
  above), and a regression would surface loudly on the next re-run rather than
  as a wrong number, so this is not a blocking gap — but the commit message
  spends a paragraph defending the behaviour, and no check defends it.
- `applied_at` defaults to `now()`, which is transaction start time, so every
  file in a single run shares one timestamp. `ORDER BY applied_at, filename` at
  `tests/test_migrate.py:71` therefore orders by filename alone. Harmless —
  apply order is genuinely covered by the `migrate(conn) == names` assertion,
  since that list is built in apply order.
- `active_transactions` was created as `SELECT *`, which Postgres froze into an
  explicit nine-column list at creation (visible in the `pg_get_viewdef` output
  above). A later `ALTER TABLE transactions ADD COLUMN` will not appear in the
  view until it is recreated. That belongs to `001_init.sql` (`4b92212`), not to
  this commit, and is recorded here only so it is written down somewhere.

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
