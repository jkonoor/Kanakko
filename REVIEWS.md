# QA Reviews — Kanakko

The QA pass prepends the newest review here, one per implementer commit. The
implementer reads this file **first** each iteration and fixes any open
(⚠️ CHANGES REQUESTED) findings before starting new work.

Status markers: **✅ DONE** — no blocking issues · **⚠️ CHANGES REQUESTED** —
open findings the next iteration must fix before anything else.

A review records what was actually checked and what the commands actually
returned, not what they were assumed to return.

---

## 2026-08-06 — `131dbe5` — guard that every production read goes through `active_transactions` (§6, task 80)

**Status: ⚠️ CHANGES REQUESTED** — the invariant genuinely holds today and the
guard reddens when the *current* read is repointed, but the guard silently
misses the two most likely ways the bypass gets reintroduced. This is the
"guard that reports safety it doesn't provide" failure mode CLAUDE.md flags as
the #1 recurring defect here: the box is now ticked and the next iteration will
trust it, while a bypass written in an ordinary style walks straight past it.

**Scope:** New `tests/test_read_paths.py` — a source-scan guard that strips
docstrings/`#` comments from `kanakko/*.py` and fails on any
`\bfrom\s+transactions\b`. `TASKS.md` tick.

**What I actually checked (commands + results):**

- `git show HEAD` / `--stat` — read the full diff; 2 files, +48/−1.
- `uv run pytest -q` → **`101 passed, 1 warning in 2.69s`** (was 100).
- **Confirmed the invariant holds.** Grepped every `FROM`/`INSERT`/`UPDATE` in
  `kanakko/*.py`: the only ledger *read* is `undo_last`'s subquery,
  `SELECT txn_id FROM active_transactions` (`db.py:163`); the writes target the
  base table (`INSERT INTO transactions` `db.py:103`, `UPDATE transactions SET
  deleted_at` `db.py:161`), which a view can't own. Correct.
- **Verified the redden-on-repoint claim.** Edited `db.py:163` to `SELECT
  txn_id FROM transactions` and ran `uv run pytest tests/test_read_paths.py -q`
  → **`1 failed`** (`AssertionError` at `test_read_paths.py:44`). Restored the
  file. So the guard does fire on the read that exists today. (The commit
  message cites `db.py:107` for this; the actual read is `db.py:163` — `:107` is
  the INSERT's `fetchone`. Cosmetic, not a finding.)
- **Tried to defeat the guard** with the test's own `sql_only`/scan logic — see
  findings below.

**Findings (ranked):**

### 1 — ⚠️ Triple-quoted SQL bypasses the guard silently (`tests/test_read_paths.py:38-40`)

`sql_only` strips **every** triple-quoted string
(`re.sub(r'""".*?"""', "", ...)`), on the assumption that only prose lives in
`"""..."""`. But a multi-line SQL query written as a triple-quoted string — the
most idiomatic way to write multi-line SQL in Python — is stripped along with
the prose, so its `FROM transactions` never reaches the scan.

Failure scenario (verified with the guard's own logic):

```python
cur.execute("""
    SELECT sum(amount) FROM transactions WHERE user_id = %s
""", (user_id,))
```

→ `sql_only` deletes the whole string; `scan` returns `[]`; the guard stays
**green** on a read that resurrects soft-deleted rows inside a sum. The
concatenated-`"..."` style `db.py` uses today is caught (I confirmed), which is
exactly why the guard passes now — but the invariant it protects breaks the
moment a future dashboard/report query (task 81+, the reads this guard exists
for) is written as triple-quoted SQL. The guard's whole reason to exist is to
catch that reintroduction, and it doesn't.

Suggested fix: don't strip triple-quoted strings wholesale. Walk the module
with `ast` and scan the values of every string literal *except* the docstring
positions (`ast.get_docstring`), or at minimum stop stripping `"""..."""` and
only strip `#` comments plus the leading module/function docstrings. Then
re-verify by pasting the triple-quoted query above into a module and watching it
redden.

### 2 — ⚠️ `JOIN transactions` is not caught (`tests/test_read_paths.py:41`)

The scan matches only `\bfrom\s+transactions\b`. Reading the base table through
a join — `FROM active_transactions a JOIN transactions t ON a.txn_id = t.txn_id`
— reads soft-deleted rows just as `FROM transactions` does (the join row set
includes deleted `t` rows), but there is no `FROM transactions` token, so the
guard stays green. Verified: `scan` returns `[]` for that string.

Suggested fix: match `\b(from|join)\s+transactions\b` (case-insensitive), and
re-verify by adding such a join and watching it redden.

Both findings are guard-coverage gaps, not defects in production code — the
current ledger read is correct. But a guard that passes on the idiomatic
reintroduction of the very bypass it names is, per CLAUDE.md, "worse than no
guard, because it stops anyone looking." Task 80 should stay open until the
guard reddens on at least the triple-quoted case.

---

## 2026-08-06 — `f7fa03d` — dedup Telegram updates by `update_id` so a redelivered `/undo` can't delete a second row (§14, review b9acac9)

**Status: ✅ DONE** — no blocking issues. The fix resolves the sole open finding
from the `b9acac9` review, is root-caused in the right place (the webhook, not
per-handler), and ships a guard that genuinely reddens without it.

**Scope:** New `processed_updates` idempotency ledger
(`migrations/002_processed_updates.sql`), `db.claim_update` (INSERT … ON CONFLICT
DO NOTHING RETURNING), and the webhook consolidated to one connection per update
that claims the `update_id` inside the handler's own transaction and skips the
handler on a repeat. New regression test.

**What I actually checked (commands + results):**

- `git show HEAD` / `--stat` — read the full diff; 5 files, +131/−16.
- `uv run pytest -q` → **`100 passed, 1 warning in 2.58s`** (was 99). The
  new test runs against the throwaway Postgres cluster in `conftest.py`, not a
  stub.
- **Reverted the fix to confirm the guard fails for its reason.** Deleted the
  `if isinstance(update_id, int) and not claim_update(...)` block from
  `kanakko/app.py:279-281` and re-ran the new test:
  `uv run pytest -q -k redelivered` →
  **`assert (0,) == (1,)` at test_webhook.py:515** — both confirmed rows soft-
  deleted, exactly the silent money-path loss the fix prevents. Restored the
  file; the other two dispatch tests stayed green (they carry no `update_id`).
  The guard asserts the **effect** (rows surviving in `active_transactions`),
  not a surface string.
- Traced the transaction semantics against the diff:
  - Claim + handler share one `with connect() as conn` (`app.py:278-292`), so
    they commit together and roll back together. A handler that raises (500)
    unwinds the block → psycopg `__exit__` rolls back → claim gone → Telegram's
    redelivery legitimately re-runs. A committed update conflicts on the PK →
    `claim_update` returns False → 200 no-op. Matches the docstring's claim.
  - The refactored `elif action.data == …` chain (`app.py:287-292`) is sound:
    `dispatch` (`app.py:72-100`) returns only `TextMessage | ButtonPress | None`;
    `None` returns at line 271 and `TextMessage` takes the first branch, so any
    value reaching the `elif`s is a `ButtonPress` with a `str` `.data`. No
    `AttributeError` risk from dropping the old `isinstance` checks.
  - `handle_undo` (`app.py:156-174`) does the DB write before `send_message`, so
    a send failure rolls the soft-delete back with the claim — net effect is
    still exactly one undo across a redelivery. No regression.
- `migrations/002_processed_updates.sql` is a new file; `001_init.sql` is
  untouched (respects "don't edit an applied migration"). `kanakko/migrate.py`
  applies `*.sql` in sorted filename order and records each in
  `schema_migrations`, so 002 is picked up exactly once. `update_id` is `BIGINT
  PRIMARY KEY` — correct width for Telegram ids and the right column for the
  ON-CONFLICT dedup. No `float`, no amount touched, no read bypassing
  `active_transactions`.

**Non-blocking observations (no change required):**

- The `§14` citation is loose: §14 is "Hosting" and only chose webhook over long
  polling; there is no numbered decision for redelivery idempotency. The webhook
  docstring already carried `(§14)` pre-commit, so this is consistent with
  existing practice, and the behaviour itself is correct and well-documented in
  the migration header. Flagging only so a future reader isn't surprised.
- The test's `_Reuse` context manager runs both deliveries on one shared
  connection (the second INSERT sees the first within the same transaction),
  whereas production uses a fresh connection per delivery (the second blocks on
  the PK until the first commits, then conflicts). Both paths dedup correctly via
  `ON CONFLICT DO NOTHING`; the test is a faithful proxy for the app logic. The
  true cross-transaction blocking path is a Postgres guarantee, not app code, so
  not worth a separate test.
- `processed_updates` grows one row per update forever; the author marked this
  with a `ponytail:` comment and a retention-sweep upgrade path. Correct call at
  personal scale (§8 — no Redis), and monotonic ids mean old rows never repeat.

**Findings:** none blocking.

---

## 2026-08-06 — `b9acac9` — add `/undo`: soft-delete the last confirmed transaction (§5, §6, task 79)

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one open
finding on the money path: a redelivered `/undo` update soft-deletes a *second*
real transaction, silently dropping it from the user's totals. The rest of the
commit is correct and its guards genuinely guard.

> **RESOLVED** in the follow-up commit on `ralph/phase-2`. Root-caused at the
> webhook, not per-handler: a redelivery carries the same Telegram `update_id`,
> so the webhook now `claim_update`s that id in the handler's own transaction
> and skips the handler when it returns False. The claim and the handler's
> writes share one transaction, so they commit together and roll back together —
> a handler that 500s legitimately re-runs on the redelivery, but a redelivery of
> a *committed* update is a no-op. This fixes `/undo` (which had no per-message
> anchor the way Confirm/Cancel do) and hardens every other handler for free
> (§14). New migration `002_processed_updates.sql` (a `processed_updates`
> idempotency ledger), new `db.claim_update`, webhook consolidated to one
> connection per update. New guard `test_a_redelivered_undo_does_not_soft_delete_a_second_row`
> seeds two confirmed rows, POSTs the identical `/undo` update twice through the
> real webhook against Postgres, and asserts exactly one survives in
> `active_transactions` — reverting the webhook's `claim_update` guard reddens it
> (`assert 1 == 0`, both rows gone) while the routing tests (no `update_id`) stay
> green. `uv run pytest -q` → **100 passed** (was 99).

**Scope:** New `db.undo_last` sets `deleted_at` on the user's newest live row
(chosen from `active_transactions`, scoped by `user_id`) and returns its fields;
`app.handle_undo` replies naming what was removed or "Nothing to undo."; the
webhook intercepts `/undo` (and `/undo@bot`) before `handle_text` so it never
reaches the parser. +7 tests, TASKS.md tick.

**What I checked (commands run, actual output):**

- `git show HEAD` — read the full diff. `undo_last` (`kanakko/db.py:146-181`)
  SELECTs from `active_transactions` (§6), scopes by `user_id` (§1), orders
  `created_at DESC, txn_id DESC` (correct: "most recent *confirmed*" = insert
  time, not `occurred_on`), and UPDATEs the base `transactions` table to set
  `deleted_at = now()` — a single atomic statement. Amount comes back as a
  `NUMERIC` → `Decimal` and reaches `format_amount` unchanged; no float on the
  path (§9). `format_amount` (`kanakko/money.py:61-63`) additionally rejects a
  float/bool with `TypeError`, so a leak would raise, not round silently.
- `uv run pytest -q` → **99 passed, 1 warning** (matches the commit's claim; the
  warning is the pre-existing Starlette/httpx testclient deprecation, unrelated).
- **Reverted the view guard** — changed `active_transactions` → `transactions`
  in `undo_last` and ran the undo tests: `test_undo_walks_back_through_history`
  **FAILED** (`AssertionError: Decimal('250.00') == Decimal('100.00')` — the
  second `/undo` re-latched onto the already-deleted newest row instead of
  walking back). Restored. The guard fails for the reason it exists.
- **Reverted the scope guard** — dropped `WHERE user_id = %s` (and its param, to
  keep the SQL valid) and ran `test_undo_is_scoped_to_the_user`: **FAILED**
  (`AssertionError: Decimal('999.99') == Decimal('100.00')` — user A's `/undo`
  removed user B's newer ₹999.99 row). Restored. Genuine §1 guard.
- Traced routing: `_is_undo` (`app.py:150-153`) splits `text`, strips an
  `@bot` suffix, lowercases — `/undo`, `/undo@KanakkoBot`, `/UNDO` all route to
  `handle_undo`; ordinary text falls through to `handle_text`. `dispatch`
  guarantees `text` is a `str`, so no `None` crash. Webhook wiring at
  `app.py:264-268`. `test_webhook_routes_undo_to_handle_undo_not_handle_text`
  covers all three cases.
- `git status --short` → clean after all reverts; no residue from the
  experiments.

**Findings:**

1. **(Medium — money path, silent) A redelivered `/undo` update removes a second
   real transaction.** `kanakko/app.py:264-268`, `kanakko/db.py:146-181`.

   Telegram redelivers any update it did not get a 2xx for (documented at
   `app.py:247` and in `confirm_pending`'s docstring). Every *other* money-path
   handler is idempotent against this: `confirm_pending` / `cancel_pending`
   operate on a specific pending row keyed by `message_id`, so once that row is
   consumed a redelivered tap is a no-op (returns `None`). `undo_last` has no
   such anchor — it deletes "the newest live row", so each redelivery removes one
   *more* row.

   Scenario: user confirms ₹250 (food) and ₹100 (transport), then sends `/undo`.
   The handler soft-deletes ₹100 and calls `send_message` (an outbound HTTP call
   to Telegram) — if that call is slow and the webhook doesn't return 200 in
   time, Telegram redelivers the same `/undo`, the handler runs again and
   soft-deletes ₹250 too. The user asked to undo one entry; two are gone. Because
   there is no user-facing un-undo, the extra row stays out of every total until
   someone edits the DB — a monthly total quietly short by ₹250, exactly the
   "total quietly off by a day's transactions" class this project treats as the
   expensive kind. Soft delete makes it recoverable in principle but not by the
   user.

   No test would catch this: the routing test stubs `handle_undo`, and no test
   drives two `undo_last` calls for one logical `/undo`. This is the first
   money-mutating handler keyed on "the newest row" rather than a specific
   message id, so it is the first one where redelivery deletes instead of
   no-ops.

   Suggested fix (design call for the implementer, not made here): dedup updates
   at the webhook by Telegram `update_id` before dispatching (a general fix that
   also hardens `handle_text`), or make `/undo` idempotent per source message —
   e.g. anchor the undo to the triggering message so a redelivery of the *same*
   `/undo` is a no-op rather than "delete the next one". Whichever, add a guard
   that reddens when two deliveries of one `/undo` delete two rows.

**Not findings (checked, deliberately not flagged):**

- Send-before-commit (`send_message` runs inside `handle_undo`, the commit is on
  `with connect()` exit) is the codebase-wide pattern — `handle_confirm` /
  `handle_cancel` also ack before commit. Pre-existing, out of scope for this
  commit.
- `get_or_create_user` creating a row for a fresh user who types `/undo` with no
  transactions is harmless and matches `handle_text`.
- Reads go through `active_transactions`; the UPDATE correctly targets the base
  `transactions` table (a view can't own the soft-delete write). Consistent with
  §6.

---

## 2026-08-06 — `00561e9` — swallow "message is not modified" 400 in edit_message_text (§5, review 9a25c4f)

**Status: ✅ DONE** — no blocking issues. The fix root-causes the redelivery
loop the prior review (`9a25c4f`) flagged, in the shared send helper, with a
guard that reddens without the fix.

**Scope:** `tg.edit_message_text` now catches `httpx.HTTPStatusError`; when it's
the Bot API 400 "message is not modified", it returns the response body as
success instead of letting `raise_for_status` propagate a 500 that Telegram
answers by redelivering the tap forever. A new `_is_not_modified` helper gates
the swallow: 400-only, description-matched, and it treats a non-JSON body as
"not the no-op" (raises). Two new tests in `test_tg.py`.

**What I checked (commands run, actual output):**

- `git show HEAD` — read the full diff; the swallow lives in the shared
  `edit_message_text` (`kanakko/tg.py:79-84`), not on the single `handle_category`
  caller, so every future editor benefits. Root-cause fix, not symptom.
- Traced the loop end to end: `webhook` (`app.py:242-244`) → `handle_category`
  (`app.py:181-208`) re-writes the *same* category, `confirm_card` re-renders
  byte-identical text+markup → `edit_message_text` → 400 not-modified. Before the
  fix this raised out of `handle_category`, the webhook 500'd, and Telegram
  (which redelivers any non-2xx, per the `webhook` docstring) re-sent the tap.
  Loop confirmed real; the fix closes it and `answer_callback_query` still fires.
- `uv run pytest -q` → **92 passed, 1 warning** (matches the commit's claim; the
  warning is the pre-existing Starlette/httpx testclient deprecation, unrelated).
- **Defeated the guard as required:** replaced the try/except with a plain
  `return _call(...)` and ran `uv run pytest tests/test_tg.py -q` →
  `test_edit_swallows_message_not_modified` **FAILED** (HTTPStatusError
  propagates, `tests/test_tg.py:109`), 7 passed. Restored → `8 passed`. The
  swallow guard fails for the reason it exists; the "still raises on other 400"
  guard pins that a genuine 400 ("chat not found") is not swallowed. Working tree
  left clean (`git status --short` empty).

**Verification notes:** The test double (`_StatusResponse.raise_for_status`
raising `HTTPStatusError`) faithfully mirrors `_call`'s real path
(`response.raise_for_status()` then `.json()`), and the mocked `httpx.post`
signature (`url, json, timeout`) matches the real call. `_is_not_modified`'s
`except ValueError` correctly covers a non-JSON error body (httpx's
`json.JSONDecodeError` subclasses `ValueError`), so a 400 with an HTML body
raises rather than being mis-swallowed. No money, timezone, soft-delete, or
`initData` surface is touched. No findings.

---

## 2026-08-06 — `9a25c4f` — handle a category button press (§5, task 78)

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one
blocking finding: an ordinary re-tap of the already-selected category 500s into
a Telegram redelivery loop — the exact trap this commit's docstring claims to
have closed.

> **RESOLVED** in the follow-up commit on `ralph/phase-2`. The identical-edit
> 400 is now swallowed in the shared `tg.edit_message_text`, not just on the
> category path: an `editMessageText` that Telegram answers with
> `400 "message is not modified"` is treated as success (the message already
> reads the way we wanted) and its body returned, instead of `raise_for_status`
> propagating a 500 that Telegram would answer by redelivering the tap forever.
> A different 400 (e.g. "chat not found") still propagates. Fixed in the shared
> send helper so every future editor benefits, not only `handle_category`.
> Two new guards in `test_tg.py`: `test_edit_swallows_message_not_modified`
> drives the real not-modified 400 through `edit_message_text` and asserts it
> returns rather than raises; `test_edit_still_raises_on_other_400` pins that a
> genuine 400 is not swallowed. Verified the swallow guard earns its place:
> reverting the try/except to a plain `return _call(...)` reddens
> `test_edit_swallows_message_not_modified` (the `HTTPStatusError` propagates);
> restoring greens it. `uv run pytest -q` → `92 passed` (was 90).

**Scope:** New `db.set_pending_category` re-writes a pending row's category
(scoped by `user_id`, round-tripped through the `Transaction` model) and returns
the updated txn; new `app.handle_category` routes a `cat:<name>` tap, edits the
card in place into a full confirm card, and answers the callback. Unknown/forged
categories are acked and ignored. `categories.CATEGORY_PREFIX` is now one
constant emitted by the keyboard and matched by the webhook dispatch.

### What I checked (commands run)

- `git show HEAD` — full diff (categories.py, db.py, app.py, test_db.py +3,
  test_webhook.py +2 & routing, TASKS.md tick).
- `uv run pytest -q` → **90 passed, 1 warning** (pre-existing httpx deprecation).
  Matches the commit's "90 passed (was 85)".
- **Verified the two claimed guards bite by reverting each:**
  - Removed the `if category not in ALL_CATEGORIES` guard from `handle_category`
    → `uv run pytest tests/test_webhook.py -k forged` → **1 failed**
    (`test_handle_category_ignores_a_forged_unknown_category`). Restored.
  - Dropped the `user_id = %s` clause from `set_pending_category`'s SELECT →
    `uv run pytest tests/test_db.py -k scoped_to_the_user` → **2 failed**
    (`test_set_pending_category_is_scoped_to_the_user` and, as a bonus, the
    confirm scoping test — the fallback `ORDER BY … DESC` picks the other user's
    row). Restored. Working tree clean, suite green again (90 passed).
- **Spec fit against §5.1:** DECISIONS §5 replaces the field editor with, first
  item, "Category buttons directly on the confirm card." The tap corrects the
  most-often-wrong field in one press. No conversation state machine introduced.
- **§9 money invariant:** round-trip is `Transaction.model_validate({**parsed,
  "category": category})` then `model_dump(mode="json")`; `test_db.py` asserts
  the stored `amount` is still the string `"250.00"`, not a float. Confirmed.
- **§11 closed set:** category is re-validated by the model round-trip and by the
  `ALL_CATEGORIES` guard. No category string literal added outside
  `categories.py`; the `cat:` prefix is now the single `CATEGORY_PREFIX`
  constant, emitted by `keyboard()` and matched by the dispatch — no drift.
- **Dispatch ordering:** `CONFIRM`/`CANCEL` are exact-match branches, category is
  `startswith(CATEGORY_PREFIX)`; `"confirm"`/`"cancel"` don't start with `"cat:"`,
  so no collision. Confirmed by reading the four `elif` branches in `webhook`.

### Findings

**1. (blocking) Re-tapping the already-selected category 500s → Telegram
redelivery loop — `kanakko/app.py:206`, via `kanakko/tg.py:40`.**

`handle_category` unconditionally calls `edit_message_text(...)` with the
freshly-rendered `confirm_card(txn)`. When the chosen category equals the row's
current category — which the full confirm card *already displays as a tappable
button* (§5.1) — the re-render is byte-identical (same text, same keyboard).
Telegram's `editMessageText` then returns `400 Bad Request: message is not
modified`. `tg._call` does `response.raise_for_status()` (`tg.py:40`), so that
400 raises `httpx.HTTPStatusError`, which propagates out of `handle_category`
and out of the `webhook` coroutine — there is no `try/except` around the
handler (`app.py:242-244`). FastAPI returns **500**, Telegram never got a 2xx,
so it **redelivers the same callback**, which 400s again → 500 again → forever.

Failure scenario (inputs → wrong result): a confirm card shows `Category: Food`
with the category buttons on it (parse returned "Food", or the user just picked
it). The user taps **Food** again — to re-affirm, or by accident. First tap:
`set_pending_category` writes Food (unchanged) → `confirm_card` renders
identical content → `editMessageText` 400 → webhook 500 → Telegram redelivers →
500 → redelivery loop; the spinner on the button never clears and
`answer_callback_query` (which runs *after* the edit) is never reached. This is
precisely "a raise here would make Telegram redeliver the bad tap forever" that
the docstring says it avoided — the unknown-category branch is guarded, but the
identical-edit branch is not.

Why the tests didn't catch it: both new webhook tests `monkeypatch` a
non-raising `edit_message_text`, so they never exercise the 400. A check that
drives the *real* not-modified condition (or asserts the handler tolerates a
400 from `edit_message_text`) would fail today and is the missing guard on this
path.

Suggested fix (root cause, one place): treat "message is not modified" as
success rather than a 500 — either short-circuit when the category is unchanged
(skip the edit, still `answer_callback_query`), or catch the specific 400 in
`handle_category`/`edit_message_text` and fall through to acking the callback.
Whichever, add a check that reddens if an identical re-render escapes as a 500.

**2. (minor, non-blocking) Cross-type forged category accepted —
`kanakko/app.py:197`.** The guard checks membership in `ALL_CATEGORIES` (the
union), not in the categories valid for `txn.type`. A forged `cat:Salary`
callback on an *expense* card passes and stores an income category on an expense
row. Only reachable by forging one's own callback_data, and the parse schema
already uses the union `enum`, so this is consistent with existing behaviour and
affects only the forger's own data — noting it, not blocking on it.

---

## 2026-08-06 — `ead8aab` — category buttons directly on the confirm card (§5, task 77)

**Status: ✅ DONE** — no blocking issues.

**Scope:** `confirm_card` now appends `category_keyboard(txn.type)`'s rows below
the Confirm/Cancel row, so the wrong-category fix is one tap on the card. Pure
rendering change; the category-press handler that mutates the pending row is the
next (still-unticked) task.

### What I checked (commands run)

- `git show HEAD` — full diff (confirm.py, test_confirm.py, TASKS.md tick).
- `uv run pytest -q` → **85 passed, 1 warning** (the pre-existing httpx
  deprecation only). Matches the commit's claim (was 84, +1).
- **Verified the new guard bites:** replaced `kanakko/confirm.py` with its
  `HEAD~1` version (`git show HEAD~1:kanakko/confirm.py`) and re-ran
  `tests/test_confirm.py` → **1 failed** on
  `test_category_buttons_are_on_the_card_for_one_tap_correction` (the assertion
  that every `cat:<name>` for the txn type is present in the keyboard). Restored
  the file; suite green again. The guard reddens for the reason it exists —
  dropping the category buttons off the card — not on a surface form.
- **Spec fit against §5.1:** DECISIONS §5 explicitly replaces the field editor
  with, first item, "Category buttons directly on the confirm card." The change
  implements exactly that. No conversation state machine introduced (§5 "Why").
- **Categories sourced from `categories.py` only:** `confirm.py` imports
  `keyboard as category_keyboard`; no literal category string in the module. The
  keyboard's `cat:<name>` data is generated from `EXPENSE_CATEGORIES` /
  `INCOME_CATEGORIES`, so the card can never offer a category the schema rejects.
- **No spec-critical surfaces touched:** amount still renders via
  `format_amount` (no float), no `active_transactions`/timezone/money path in
  scope, no confidence score, `category` nullability untouched. The
  `assert txn.category is not None` precondition still routes null categories to
  `category_prompt` (§3).
- Confirmed `git status` clean after the revert experiment; no stray edits left.

### Findings

None. The change is minimal, matches §5.1, keeps categories single-sourced, and
ships a guard that genuinely fails without the fix. The category-press handler is
correctly left as the next unticked task, so nothing is falsely ticked.

---

## 2026-08-06 — `6ac1790` — show category buttons when the parse returned no category (§3, task 73)

**Status: ✅ DONE** — no blocking issues.

**Scope:** When `parse_message` returns `category: null`, `handle_text` now
renders a category picker (`category_prompt`) instead of the Confirm/Cancel
confirm card; the pending row is still written keyed by the sent message id.
`confirm_card` gains an `assert txn.category is not None` backstop.

### What I checked (commands run)

- `git show HEAD` — reviewed the full diff (app.py, confirm.py, categories.py
  usage, test_webhook.py, TASKS.md tick).
- `uv run pytest` → **84 passed, 1 warning** (the pre-existing httpx
  deprecation warning only). Full suite green.
- `uv run pytest tests/test_webhook.py` → 18 passed.
- **Verified the new guard bites:** replaced line 135–136 of `app.py` with an
  unconditional `text, keyboard = confirm_card(txn)` and re-ran
  `test_handle_text_shows_category_buttons_when_category_is_null` →
  **1 failed** (the null category reaches `confirm_card` and trips its assert /
  sends Confirm not `cat:`). Restored `app.py` via `git checkout`; suite green
  again. The test is a real check, not a rubber stamp.
- Confirmed routing completeness: `grep` shows the only production caller of
  `confirm_card` is `app.py:135`, correctly guarded by `txn.category is None`;
  `category_prompt` is likewise only reached from there. No path lets a null
  category into `confirm_card` in production.
- Confirmed `category_keyboard(txn.type)` cannot `KeyError`: `parse.py:123`
  types `type: Literal["expense", "income"]`, both keys of `CATEGORIES_BY_TYPE`.
- Spec fit: §3 ("`category: null` → show the category buttons") — matches.
  Categories sourced only from `categories.py` (imported `keyboard`), no literal
  category strings. `amount` still non-nullable, `category` nullable, no
  confidence score introduced. No money/timezone/soft-delete surface touched.

### Findings

None blocking. Three low-severity observations, all recoverable / out of the
task's scope — recorded, not requested:

1. **No Cancel on the picker** (`confirm.py:49` `category_prompt`). The picker
   offers only the `cat:` buttons — a user who spots a wrong *amount* at this
   point cannot abort directly; they must pick any category and Cancel on the
   resulting confirm card (task 78, not yet built). §5 allows Cancel-and-retype;
   this adds one tap to that path. Acceptable for now since task 78 owns the
   post-pick card; worth a Cancel button if that card doesn't materialise.
2. **Date line dropped** (`confirm.py:58–61`). `category_prompt` shows
   type/amount/note but not `Date:` (the confirm card shows it). No data loss —
   the parsed date is persisted in the pending row and shown on task 78's card.
   Cosmetic.
3. **`assert` stripped under `python -O`** (`confirm.py:31`). The assert is a
   fail-loud backstop, not the real guard — the routing in `app.py:135` is what
   actually keeps a null out of `confirm_card`, and that is not an assert. So
   `-O` weakens only the redundant backstop, not the behaviour. No action needed.

---

## 2026-08-06 — `0667bfc` — reject an unparseable message with a rephrase prompt (§3, task 72)

**Scope:** `handle_text` now catches the `ValidationError` that `parse_message`
raises when the amount can't be read, sends `REPHRASE_PROMPT`, and returns
`None` instead of letting it propagate to a 500 (which made Telegram redeliver
the same unparseable text forever). Return type widened to `int | None`. New
guard test.

**Status: ✅ DONE** — no blocking issues.

### What I checked (commands run)

- `git show HEAD` — reviewed the full diff (`TASKS.md`, `kanakko/app.py`,
  `tests/test_webhook.py`).
- Read `kanakko/parse.py`, `kanakko/money.py`, and `docs/DECISIONS.md` §2–§3
  to confirm the exception path and the spec.
- `uv run pytest -q` → **83 passed**, matching the commit message.
- `uv run pytest tests/test_webhook.py -q` → 17 passed.
- **Guard verified by reverting:** removed the `try/except` from `handle_text`
  and ran `pytest -k rephrase` → the new test **FAILED** (the `ValidationError`
  propagated instead of returning `None`). Restored `app.py` with
  `git checkout`. The guard fails for the reason it exists.

### Spec fit

- §3 says "no amount means no transaction, so reject the message and ask for a
  rephrase rather than showing a confirm card with a blank." The change does
  exactly that: `send_message(msg.chat_id, REPHRASE_PROMPT)` then `return None`,
  `save_pending` never reached. The test asserts `pending_count == 0` and
  `reply_markup is None` (a prompt, not a confirm card) — the right effects.
- Exception path traced end to end and confirmed real, not just the mock: an
  empty/missing amount → `money.parse_amount("")` raises `ValueError("amount is
  empty")` → the `_amount_is_exact` field validator re-raises as `ValueError` →
  Pydantic surfaces it as `ValidationError` → `parse_message` re-raises after its
  one retry (`parse.py:182-183`) → caught at `app.py:127`. The test's
  `raise_validation` reproduces this faithfully by validating a real
  `amount=""` payload rather than raising a bare exception.
- Webhook path: `handle_text` returning `None` leaves `webhook` returning
  `{"ok": True}` (200), so Telegram does not redeliver — the stated goal.

### Notes (non-blocking, no fix required)

- The catch is narrowed to `ValidationError` only. On the second (retry) attempt
  `parse_message` can also raise `json.JSONDecodeError`, `httpx` errors, or
  `RuntimeError` (missing key) — none of which are caught, so they still 500 and
  Telegram redelivers. This is correct: those are transient transport/schema
  failures, not "no amount," and redelivery is the right response for them
  (`parse.py` docstring says as much). The scoping matches §3's intent.
- `get_or_create_user` runs before the parse, so an unparseable first message
  still creates (and, on `webhook` block-exit, commits) a user row. This is
  benign — the user row is not a transaction, it's idempotent, and the same row
  is created on any first message. Not a §3 "store nothing" violation.
- No money/`float`, timezone, `active_transactions`, or `initData` surface is
  touched by this commit.

---

## 2026-08-06 — `1426440` — handle Cancel: discard the pending row, acknowledge (§5, task 65)

**Scope:** Wire the CANCEL button tap into `/webhook`. New `db.cancel_pending`
deletes the user's pending row for the card's `telegram_message_id` (scoped by
`user_id`, §1) and returns the deleted `pending_id` or `None` on a redelivered
tap. New `app.handle_cancel` resolves the user, cancels, and answers the
callback query ("Discarded ❌" / "Already gone"). Nothing is written to the
ledger (§5).

**Status: ✅ DONE** — no blocking issues. One low-severity note below; it does
not change behaviour or require a fix before the next task.

### What I checked (commands run)

- `git show HEAD` — reviewed the full diff (`TASKS.md`, `kanakko/app.py`,
  `kanakko/db.py`, `tests/test_webhook.py`).
- `uv run pytest -q` → **82 passed, 1 warning** (the pre-existing Starlette
  `httpx` deprecation, unrelated). Matches the commit message's claim.
- **Reddened the scoping guard myself** to confirm it fails for the reason it
  exists: temporarily dropped `AND user_id = %s` from `cancel_pending`'s DELETE
  and reran `tests/test_webhook.py::test_cancel_is_scoped_to_the_user` →
  **`assert 0 == 1` (FAILED)** — A's Cancel deleted B's identically-numbered
  card. Restored `db.py` (`git checkout`) and reran the suite → 82 passed. The
  guard is real, not a surface-form assertion.
- Read `docs/DECISIONS.md` §1 (every table keyed on internal `user_id`) and §5
  (Cancel discards, no ledger write, no state machine) — the change matches
  both.

### Correctness and spec fit

- **§5 honoured.** `handle_cancel`/`cancel_pending` only DELETE the pending row
  and ack; no `transactions` insert, no conversation state. The
  `test_handle_cancel_discards_the_pending_row_and_acknowledges` test asserts
  `ledger_count == 0` via `active_transactions` — the right assertion, since a
  stray write would show up there.
- **§1 scoping** mirrors the already-reviewed `confirm_pending`: DELETE is keyed
  on `(user_id, telegram_message_id)`, verified reddening above.
- **Idempotent redelivery** (§5-adjacent, matches the Confirm handler's
  contract): a second tap returns `None` and still acks "Already gone", so
  Telegram's spinner clears on the redelivered 200. Covered by
  `test_handle_cancel_is_idempotent_on_a_redelivered_tap`.
- **Routing** is asserted both ways in the rewritten
  `test_webhook_routes_confirm_and_cancel_to_their_handlers`: CONFIRM→confirm,
  CANCEL→cancel, never crossed; each button tap opens exactly one connection; an
  ignored update opens none. Good — a CANCEL leaking to `handle_confirm` would
  write a ledger row, and that path is now guarded.
- **Commit semantics:** the endpoint uses `with connect() as conn:`, identical
  to the Confirm path, so the DELETE commits on clean exit and a handler
  exception rolls back and 500s for redelivery. `cancel_pending` correctly does
  not commit itself.
- No `float`, no amount arithmetic, no read of `transactions`/`active_transactions`
  in this diff — none of the money/timezone axes are touched.

### Low-severity note (no fix required)

1. `kanakko/app.py:138` — `handle_cancel` acknowledges but does **not**
   `edit_message_text` to strip the Confirm/Cancel buttons off the discarded
   card. Not a spec violation (§5 asks only to discard + acknowledge) and it is
   safe: the pending row is gone, so a later Confirm tap on the same lingering
   card hits `confirm_pending`→`None` and writes nothing. Worth a follow-up only
   if the stale card's buttons prove confusing in use; leaving it is the correct
   lazy call for this task.

### Falsely-ticked check

Task 65 is genuinely done: real behaviour (DB delete + ack), real error/edge
handling (idempotent redelivery, user scoping), and guards that fail for their
stated reason. Not a stub.

---

## 2026-08-06 — `fbd351f` — wire Handle Confirm into `/webhook` (§4, §14, §15, task 64)

**Scope:** Route a dispatched `TextMessage` → `handle_text` and a
`ButtonPress(data=CONFIRM)` → the new `handle_confirm`, each inside a
per-update `with connect() as conn:` block; ignored/redelivered/CANCEL updates
open no connection. `handle_confirm` resolves the user, calls
`db.confirm_pending`, and answers the callback query.

**Status: ✅ DONE** — no blocking issues. Two low-severity notes below; neither
changes behaviour or requires a fix before the next task.

### What I checked (commands run)

- `git show HEAD` — reviewed the full diff (TASKS.md, `kanakko/app.py`,
  `tests/test_webhook.py`).
- `uv run pytest -q` → **79 passed, 1 warning** (the pre-existing Starlette
  `httpx` deprecation). The three new/updated webhook tests are in that count.
- Read `app.py`, `db.py`, `tg.py`, `confirm.py`, `conftest.py` end to end to
  trace the real commit/redelivery flow, not just the diff.
- `uv run python -c "import psycopg; print(psycopg.__version__)"` → **3.3.4**,
  to confirm the `with connect() as conn:` commit-on-clean-exit contract the
  webhook depends on. (An empirical two-cluster probe hung on parallel
  `pg_ctl`; the conftest fixture already exercises the same context-manager
  contract, so the reliance is sound.)
- Spec fit: §15 (`app.py:21-33` origin verification unchanged, fails closed,
  `hmac.compare_digest`), §14 (always-200 on malformed body / ignored update),
  §1 (`confirm_pending` scopes the write by `user_id`), §4/§6/§9 (money path).

### Guards — do they bite?

- **CONFIRM discrimination** (`test_webhook_routes_confirm_but_not_cancel`):
  routing a CANCEL to `handle_confirm` would push `confirmed` to 2, and opening
  a connection for an ignored update would push `opened` past 1 — both asserted,
  both would fail on regression. Real guard.
- **Idempotency** (`test_handle_confirm_is_idempotent...`): drives the *real*
  `confirm_pending` against Postgres twice on the same tap and asserts
  `count == 1` via `active_transactions` plus acks `["Saved ✅", "Already
  saved"]`. A double-write would make it 2. Real guard.
- **Money path** (`test_handle_confirm_writes_the_ledger_row...`): asserts the
  row reads back as exact `Decimal("100.00")` through `active_transactions`
  (§6/§9), not `transactions` directly. Real guard.

I confirmed idempotency-plus-500 is correct under both possible psycopg
transaction semantics: whether `confirm_pending`'s inner `with conn.transaction()`
commits the money write independently (delivery 1 saves, ack fails → 500 →
delivery 2 sees no pending → "Already saved") or defers to the outer block
(delivery 1 rolls back on the ack failure → delivery 2 re-confirms), the net is
exactly one ledger row. No torn write either way.

### Notes (low severity, non-blocking)

1. **`handle_confirm` docstring says "Does not commit — the caller owns the
   transaction" (`app.py:121`), but `confirm_pending` commits internally** via
   its own `with conn.transaction()` on a fresh production connection (the
   outermost transaction block → BEGIN…COMMIT). The behaviour is correct — and
   arguably better, since the insert+delete money write is committed atomically,
   independent of the Telegram ack — but the comment describes a transaction
   ownership that isn't what actually happens. Doc nit only.

2. **No test exercises the webhook's own commit for `handle_text`.** Both
   webhook routing tests stub `connect` with `_FakeConn` (whose `__exit__`
   commits nothing), and the `handle_confirm` DB tests call the handler directly
   rather than through `/webhook`. So "the pending row is actually persisted by
   the `with connect()` block" rests on the psycopg3 contract, untested here.
   If that block ever silently stopped committing (e.g. an autocommit change),
   the failure would be invisible: the pending row is lost, the user taps
   Confirm, and `confirm_pending` returns `None` → "Already saved" with nothing
   saved. Worth one end-to-end webhook test against real Postgres asserting a
   `pending_transactions` row exists after the POST returns 200. Not blocking —
   the contract holds at 3.3.4.

Out of scope (correctly deferred, not findings): CANCEL taps get no ack yet
(task 65, unchecked), and an unparseable message currently raises → 500 →
Telegram redelivery until the "reject unparseable amount" task lands. Both are
the next unchecked tasks; missing ≠ wrong.

The TASKS.md tick for task 64 is justified — the endpoint opens the connection,
routes CONFIRM, commits, and acks, with real-DB tests behind each claim.

---

## 2026-08-06 — `c6024de` — add `app.handle_text` + `db.get_or_create_user` (§1, §2, §4, task 58 split)

**Scope:** Split the text-message half of the core loop out of Handle Confirm.
`app.handle_text(conn, msg)` resolves the user, parses the text, sends the
confirm card, and writes the pending row keyed by the *sent card's* message id.
New `db.get_or_create_user` maps a Telegram id to the internal `users.user_id`
(idempotent upsert). Standalone — not yet wired into `/webhook`.

**Status: ✅ DONE** — no blocking issues.

### What I checked (commands run, not assumed)

- `uv run pytest -q` → **76 passed** (was 74), matching the commit message.
- **Guard 1 bites.** Edited `app.py` to key `save_pending` on `msg.message_id`
  instead of `card_message_id`, then ran
  `test_handle_text_keys_the_pending_row_on_the_sent_card` → **1 failed**
  (`assert card_message_id == 909`). Reverted; passes again. The test drives the
  real `get_or_create_user`, `confirm_card`, and `save_pending` against a real
  Postgres cluster (only `parse_message`/`send_message` stubbed), so it exercises
  the actual persistence path, not a mock of itself.
- **Guard 2 bites.** Dropped `ON CONFLICT (telegram_user_id) DO NOTHING` from the
  CTE, then ran `test_get_or_create_user_is_idempotent` → **1 failed**
  (`psycopg.errors.UniqueViolation` on the second call). Reverted; passes again.
- Traced the money path: `handle_text` → `save_pending` stores
  `txn.model_dump(mode="json")`, and the test asserts `parsed["amount"] ==
  "500.00"` (a string, §9). No `float` touches the amount. ✅
- Confirmed the CTE returns the right id on both paths: on a fresh insert `ins`
  yields the new row and the sibling `SELECT` sees nothing (statement snapshot),
  so `UNION ALL … LIMIT 1` returns the inserted id; on conflict `ins` is empty
  and the `SELECT` returns the existing row. The idempotency test confirms one
  row, same id, distinct ids for distinct users.
- **"Failed send leaves no pending row" holds structurally.** `tg.send_message`
  → `_call` calls `response.raise_for_status()`, so an HTTP error raises before
  `save_pending` is reached; an API-level `ok:false` (HTTP 200) instead raises
  `KeyError` on `sent["result"]` — either way no pending row is written, the safe
  direction the docstring claims.
- `git status` clean after the two revert experiments (both files restored to
  HEAD).

### Non-blocking observations (do not fix now)

1. **`kanakko/db.py:40` — `ON CONFLICT DO NOTHING` upsert has the classic
   concurrent-first-insert race.** If two transactions insert the *same* new
   `telegram_user_id` concurrently, the loser's `DO NOTHING` returns 0 rows while
   its sibling `SELECT` (statement-start snapshot) can't yet see the winner's
   uncommitted row → `fetchone()` returns `None` → `(user_id,) = None` raises
   `TypeError`. Only reachable on two truly-parallel *first* messages from one
   brand-new user; the send is sync and single-user today (§14 deferred), so this
   is informational, not a scale finding to action now. If it ever wires up under
   concurrency, the standard fix is a retry loop or a follow-up `SELECT` after a
   commit boundary.
2. No test for the "failed send → no pending row" direction. The behaviour is
   structurally guaranteed by call ordering (send raises before `save_pending`),
   so a test is optional; noting it only so the claim isn't mistaken for tested.

### TASKS.md

The tick is honest: task 58 was genuinely split, its scope narrowed to the
wiring, and the box marks only the standalone handler that now exists and is
tested. The follow-on Handle Confirm task correctly retains the `/webhook`
routing, Confirm handler, and connection open/commit.

## 2026-08-06 — `5a19d43` — add `kanakko/tg.py` — Telegram send client (§4, §14, task 51)

**Scope:** New thin Telegram Bot API send client (`answer_callback_query`,
`send_message`, `edit_message_text`) built on raw `httpx`, mirroring
`parse.call()`. Token from `TELEGRAM_BOT_TOKEN`, fails closed when unset.
`reply_markup` serialises a `confirm_card` `InlineKeyboardMarkup` via
`.to_dict()`. New `tests/test_tg.py` (6 checks, no network). `TASKS.md` tick +
Handle-Confirm note update. No production code path is wired to it yet.

**Status: ✅ DONE** — matches the conventions, the two guards that matter
provably bite, nothing regressed. No blocking findings.

### What I checked

- **Suite green.** `uv run pytest -q` → **74 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation). Matches the claimed count (was 68;
  +6 from the new file).
- **Fail-closed guard bites for its reason.** Neutered the guard in `tg._call`
  (replaced `raise RuntimeError(...)` with `token = "x"`) and reran
  `uv run pytest tests/test_tg.py -q` → **1 failed, 5 passed**, the failure being
  `test_fails_closed_without_a_token` (`Failed: posted with no token set`).
  Restored the file; tree clean. So an unset token can never reach the network —
  the secret-from-env / never-a-literal convention holds and is enforced by a
  check that genuinely reddens.
- **Keyboard-serialisation guard is real, not a spelling check.**
  `test_keyboard_is_serialised_to_a_plain_dict` asserts the sent `reply_markup`
  is `kb.to_dict()` with `inline_keyboard[0][0]["callback_data"] == "ok"` — the
  actual Bot API nested-list shape, not merely "a dict". A raw
  `InlineKeyboardMarkup` handed to `httpx(json=...)` would not serialise, and
  this check catches that regression. Confirmed `confirm.py:confirm_card` really
  returns an `InlineKeyboardMarkup`, so the `.to_dict()` contract matches its
  one real caller-to-be.
- **Spec fit.** §4/§5 confirm-card flow uses these three methods; no
  `parse_mode` is set, which is correct — `confirm_card` is deliberately plain
  text (note is the user's own wording, §4). Sync httpx carries a `ponytail:`
  comment naming the §14 deferred throughput ceiling (~50k users → rate-limited
  send loop, not a queue) — consistent with the deferred table. No money,
  timezone, `active_transactions`, or category surface is touched, so those
  axes are N/A here.
- **Not a stub / not a falsely-ticked task.** All three methods do real work;
  the task's every claim (methods, raw httpx, env token, fail-closed,
  `.to_dict()`, no-network tests) is present and exercised. HTTP errors
  propagate (`test_http_error_propagates`).

### Non-blocking observations (no action required)

- **Application-level `ok: false` isn't inspected.** `_call` calls
  `response.raise_for_status()` and returns `response.json()` without checking
  the Bot API `ok` field (`kanakko/tg.py:36-38`). This is safe today because the
  Bot API returns a 4xx HTTP status alongside `ok: false` for errors (verified
  against the Bot API docs, 2026-08-06), so `raise_for_status()` already raises.
  Worth a glance if a future handler starts branching on the returned dict, but
  nothing silently wrong now.

---

## 2026-08-06 — `d0d97a5` — add `TELEGRAM_WEBHOOK_SECRET` to `.env.example` and compose (§15, task 43)

**Scope:** Wire the §15 webhook secret through both config sources —
`.env.example` gains `TELEGRAM_WEBHOOK_SECRET=` with the §15 alphabet note, and
`docker-compose.yml`'s shared `x-app-env` declares it with `:?see .env.example`
so a missing secret stops `up`. Task 43 ticked. No code change.

**Status: ✅ DONE** — matches §15, the cross-file guard provably bites, nothing
regressed. No findings.

### What I checked

- **Suite green.** `uv run pytest -q` → **68 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation). Unchanged count, as claimed.
- **Guard bites for its stated reason.** Removed the compose line and reran
  `uv run pytest tests/test_compose.py -q` → **1 failed, 9 passed**, failing
  exactly `test_env_example_lists_no_key_no_service_consumes` with
  `AssertionError: TELEGRAM_WEBHOOK_SECRET is in .env.example but no service
  consumes it`. Restored via `git checkout` and confirmed `git status` clean.
  This is a real bidirectional guard, not a surface-string assert: `COMPOSE_VARS`
  is derived by regex over the *actual* `${VAR}` interpolations and `ENV_KEYS`
  from a *parsed* .env, so the check tracks what compose really consumes, not a
  spelling. The commit's guard claim is accurate.
- **Spec fit (§15).** DECISIONS §15 requires the key **required** with `:?`
  (docker-compose.yml:22 ✓), a 403 on absent/wrong/unset header, and alphabet
  `A-Za-z0-9_-`, 1–256 chars. `.env.example:24-27` documents exactly that
  alphabet and length, and correctly describes the header
  `X-Telegram-Bot-Api-Secret-Token` and the fail-closed behaviour. No value is
  committed (`test_env_example_holds_no_values` covers this, still green).
- **The consumer exists.** `kanakko/app.py:26` reads
  `os.environ.get("TELEGRAM_WEBHOOK_SECRET")` and `scripts/push-env.py` validates
  and pushes it — so the key the operator is now told to set is genuinely wired,
  which is the whole point of task 43 following task 42.
- **No money/timezone/soft-delete/`initData` surface** in this diff — it is pure
  config wiring, so those decision classes are not in scope here.

### Findings

None. Config-only change, spec-accurate, guard verified to fail for the reason
it exists, full suite green.

---

## 2026-08-06 — `f565137` — verify `/webhook` origin with `secret_token`, fail closed (§15, task 42)

**Scope:** `/webhook` now rejects any request whose
`X-Telegram-Bot-Api-Secret-Token` header does not match
`TELEGRAM_WEBHOOK_SECRET` with 403, *before* the body is read or `dispatch`
runs. New `_origin_is_verified` helper; three new webhook guards. Task 42
ticked; task 43 (`TELEGRAM_WEBHOOK_SECRET` into `.env.example`/compose) left
open, correctly.

**Status: ✅ DONE** — matches §15 point for point, the fail-closed guard
provably bites, and nothing regressed. One low-severity robustness note below,
not blocking.

### What I checked

- **Suite green.** `uv run pytest -q` → **68 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation; was 65). Matches the commit's claim.
- **The fail-closed guard fails for the reason it exists — verified by
  reverting.** I temporarily removed the `if not secret: return False` clause
  (`app.py:27-28`) and reran `tests/test_webhook.py`: **exactly**
  `test_webhook_fails_closed_when_the_secret_is_unset` failed
  (`403 != 200` — with the secret unset the endpoint accepted a forged POST),
  the other 8 stayed green; restoring the clause → 9 passed. So the guard pins
  the exact §15 "unset must never mean accept-everything" behaviour, and it is
  the new test that pins it. Working tree restored, `git diff` clean.
- **Spec fit against §15 (DECISIONS.md:320-352).** Header name
  `X-Telegram-Bot-Api-Secret-Token` ✓ (`app.py:15`). Env key
  `TELEGRAM_WEBHOOK_SECRET` ✓. 403 when header absent or mismatched ✓ (two
  guards). `hmac.compare_digest`, not `==` ✓ (`app.py:30`). Fails closed on
  unset secret ✓ (`app.py:27-28`, returns `False` before the compare, so an
  empty presented token can't sneak past an empty secret under `==`). Check
  runs **before** `request.json()` ✓ (`app.py:98-102`), so a forged POST never
  buffers a body or reaches `dispatch`.
- **Guard quality.** The three new tests assert the *behaviour* (status 403 on
  missing / wrong / unset), not a surface string. The unset-secret test even
  presents an empty token to prove the empty-vs-empty `==` trap is closed. Real
  guards, not spelling checks.
- **No convention violations.** Secret read from env at request time, never a
  literal; no ORM/Redis/etc. touched; no money or timezone path involved.

### Findings

**Low — non-ASCII presented header 500s instead of 403 (`app.py:30`).** Not
blocking. `hmac.compare_digest` on two `str` raises `TypeError` on non-ASCII
input (verified: `comparing strings with non-ASCII characters is not
supported`). Starlette decodes header values as latin-1, so a raw client
sending a header byte in 0x80–0xFF reaches `_origin_is_verified` as a non-ASCII
`str` and the `TypeError` propagates to an unhandled **500** rather than the
403 the docstring promises. **Impact is contained:** the request is still
rejected before `dispatch`, so no rows are forged and the security goal holds;
legitimate Telegram traffic never triggers it (its `secret_token` alphabet is
`A-Za-z0-9_-`). Only cost is a noisier 500 on malformed forged requests.
Suggested fix if tightened later: `return False` on the `TypeError` (or compare
on `.encode()` bytes). Left as a note, not a change request.

**Nit — no test proves the body is never read.** The "before the body is read"
property (a real anti-DoS point: don't buffer an unbounded forged body) is
correct in the code ordering but untested. A `malformed body + missing secret →
403` case would pin it. Optional.

---

## 2026-08-06 — `1c979f4` — scope `confirm_pending` by `user_id` (§1, fixes `a22a437` Finding 1)

**Scope:** Resolves the sole blocking finding from the `a22a437` review.
`confirm_pending` now takes `user_id` and its `SELECT` is
`WHERE user_id = %s AND telegram_message_id = %s`, mirroring `save_pending`'s
scoping. Two existing db tests updated to pass `user_id`; one new guard,
`test_confirm_is_scoped_to_the_user`. No production caller yet (task 63 wires
the handler), so the signature change is safe.

**Status: ✅ DONE** — the cross-tenant write is closed, the guard provably
bites, and nothing else regressed.

### What I checked

- **Suite green.** `uv run pytest -q` → **65 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation; was 64). Ran against a real
  throwaway Postgres via `conftest.py`, not a mock.
- **The guard fails for the reason it exists — verified by reverting.** I
  temporarily dropped the `AND user_id = %s` clause (kept the signature) and
  reran `tests/test_db.py`: `test_confirm_is_scoped_to_the_user` **failed** with
  `AssertionError: (Decimal('999.99'), 3) == (Decimal('100.00'), 3)` — i.e. A's
  Confirm wrote B's ₹999.99 to A's ledger, exactly the wrong-owner scenario §1
  warns against. Restoring the clause → **4 passed**. The other three db tests
  stayed green under the revert, so the new test is what pins the fix.
- **§1 scoping matches `save_pending`.** `confirm_pending`'s `WHERE user_id = %s
  AND telegram_message_id = %s` (`db.py:67-70`) reverses the `(user_id,
  telegram_message_id)` `save_pending` stores. The test uses the returned
  `a`/`b` user ids, not hardcoded integers, so it's robust to the identity
  sequence.
- **§9 money intact.** `_txn(amount)` still routes through
  `Transaction.model_validate`; the scoped test asserts A gets exactly
  `Decimal("100.00")`, not B's amount — a `Decimal`, not a float.
- **§6 respected.** The verification read is on `active_transactions`
  (`test_db.py:126`); `confirm_pending`'s own `SELECT` is on
  `pending_transactions` (no soft-delete column, so §6 doesn't apply).
- **No production caller.** `grep confirm_pending` outside tests → only the
  definition and docstrings. Signature change breaks nothing; task 63 will call
  it with the `user_id` from the callback query.
- **Atomicity unchanged.** read → insert → delete still inside one
  `conn.transaction()`. No new dependency, no ORM, no float.

### Notes (non-blocking, carried forward)

- The prior review's second note still stands: no `UNIQUE (user_id,
  telegram_message_id)` on `pending_transactions`, so a double `save_pending`
  for one card would leave `confirm_pending` confirming the newest and orphaning
  the rest. Not reachable today (one card → one row); a migration-only
  hardening for whoever wires task 63.

---

## 2026-08-06 — `a22a437` — add `kanakko/db.py`, confirm-flow persistence (§1, §6, §9, prereq of task 56)

**Scope:** New `kanakko/db.py` with `connect()`, `save_pending()`, and
`confirm_pending()` — the DB half of task 56 (confirm → write to `transactions`,
clear the pending row), carved off ahead of the not-yet-built message handler and
Telegram-send client. New `tests/test_db.py` (3 checks against a real Postgres),
the `conn` fixture moved to `tests/conftest.py` (one definition, shared with
`test_migrate`). `TASKS.md`: db.py ticked, task 56 left open with remaining work
recorded.

**Status: ⚠️ CHANGES REQUESTED → ✅ RESOLVED** (see the block below) — one
latent cross-tenant defect in `confirm_pending`; everything else is sound and
the money guard bites.

> **RESOLVED** in the follow-up commit on `ralph/phase-1`. `confirm_pending` now
> takes a `user_id` and its SELECT is `WHERE user_id = %s AND telegram_message_id
> = %s`, matching `save_pending`'s scoping — a user's Confirm can no longer latch
> onto another user's identically-numbered pending card. New guard
> `test_confirm_is_scoped_to_the_user`: A (seeded first, so the *older* row) and B
> both hold a pending card on message id 555 with different amounts; A confirms
> and must get their own ₹100 with only A's pending row cleared. A is deliberately
> the older row, so the fallback `ORDER BY created_at DESC, pending_id DESC` picks
> B's newer row — the test only passes when the `user_id` clause resolves A's tap
> to A's row. Verified it earns its place: dropping the clause back to
> `WHERE telegram_message_id = %s` reddens exactly this test (A latches onto B's
> ₹999.99 row); restoring greens it. `uv run pytest -q` → `65 passed` (was 64).
> No production caller yet (task 63 wires it), so the signature change is safe.

### What I checked

- **Suite green.** `uv run pytest -q` → **64 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation; was 61). The three new db tests ran
  against a real throwaway Postgres, not a mock.
- **§9 money guard verified live, not trusted.** `Transaction.model_dump(mode="json")`
  serializes `amount` to the string `'1234.56'` (confirmed by running it), and
  feeding a JSON *number* (`1234.56` float) back through `Transaction.model_validate`
  raises `ValidationError` (confirmed). So a float in the stored JSON is refused at
  the §9 door on the way back in, never rounded into the ledger. The round-trip test
  asserts the amount returns as exact `Decimal("1234.56")`.
- **§6 respected.** The verification read in `test_db.py:48` goes through
  `active_transactions`, not `transactions`. `confirm_pending`'s own `SELECT` is on
  `pending_transactions` (unfiltered by design — pending rows have no `deleted_at`),
  so §6 doesn't apply there.
- **Atomicity is real.** read → insert → delete run inside one
  `conn.transaction()` (`db.py:59`), so a crash can't store a txn while leaving its
  pending row live, nor clear the pending row with nothing stored.
- **Idempotency holds within a user.** `test_confirm_is_idempotent_on_redelivery`
  confirms a second tap returns `None` and the ledger still holds one row. Correct
  for the current single-user deployment (per-chat message ids don't repeat).
- **Column mapping correct.** `txn.date` → `occurred_on`, `type`/`category`/`note`
  pass through; `created_at`/`deleted_at` take their defaults (now / NULL).
- **No new dependency, no ORM, no float.** Plain psycopg + `Jsonb`. `connect()`
  fails closed when `DATABASE_URL` is unset.
- **Fixture move is a clean dedup.** `conftest.py`'s `conn` is the same
  socket-only, `fsync=off` throwaway cluster `test_migrate` used; `test_migrate`
  now imports it implicitly and still passes.

### Finding 1 (blocking) — `confirm_pending` keys on `telegram_message_id` alone, single-tenant by accident (§1)

`kanakko/db.py:49,60-63` — `confirm_pending(conn, telegram_message_id)` takes no
`user_id` and selects `WHERE telegram_message_id = %s ORDER BY created_at DESC
LIMIT 1`. Telegram message ids are unique *per chat*, not globally — every user's
chat reuses the same small integers. `save_pending` correctly stores `user_id`;
`confirm_pending` throws that scoping away.

**Failure scenario (with a second user, which §1 says to build for now):** User A
and User B each have a live pending confirm card that happened to land on message
id `555`. B taps Confirm on *their* card. `confirm_pending(conn, 555)` selects the
globally-most-recent pending row for `555` — A's — and writes **A's** transaction
to A's ledger, deleting A's pending row. B's own row stays live and B's tap wrote
nothing of theirs. A wrong-owner write plus a broken idempotency guarantee, and it
raises nothing.

This is exactly the §1 anti-pattern ("never single-tenant by accident"): the whole
reason every table carries `user_id` from day one is so the multi-user change stays
small and no query bakes in a single-tenant assumption. This one does, and it's the
foundation the confirm handler (task 56) will call. It does **not** bite today's
single-user, constant-`user_id` deployment — flagging it now because the fix is one
parameter and one clause, and it gets more expensive once the handler is wired.

**Suggested fix:** give `confirm_pending` a `user_id` parameter (the handler has it
from the callback query) and add `AND user_id = %s` to the `SELECT`, matching
`save_pending`'s scoping. `test_confirm_is_idempotent_on_redelivery` should grow a
sibling: a second user with the same message id must not confirm the first user's row.

### Notes (non-blocking)

- **Multiple pending rows per `(user, message_id)` are silently tolerated.** The
  `ORDER BY … LIMIT 1` means if `save_pending` were ever called twice for the same
  card, `confirm_pending` confirms the newest and orphans the rest. Not reachable
  today (one card → one pending row), but a `UNIQUE (user_id, telegram_message_id)`
  on `pending_transactions` would make the key a real key rather than a convention.
  Migration-only; note for whoever wires task 56, not required now.

---

## 2026-08-06 — `2c6f27c` — render the confirm card (§4, §5, task 55)

**Scope:** New `kanakko/confirm.py` with `confirm_card(txn)` — a pure renderer
turning a validated `Transaction` into the card text (type · amount · category ·
date · note) plus a Confirm/Cancel inline keyboard. New `tests/test_confirm.py`
(4 checks). `TASKS.md` task 55 ticked. No rows written, no network, no schema —
sending, the pending row, and the button handlers are tasks 56/57/59.

**Status: ✅ DONE** — no blocking issues. One low-severity note below.

### What I checked

- **Suite green.** `uv run pytest -q` → **61 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation). Matches the commit's claim (was 57).
- **§9 money guard actually reddens — verified by breaking it.** I edited
  `confirm.py:32` to interpolate `{txn.amount}` instead of
  `{format_amount(txn.amount)}` and ran `pytest tests/test_confirm.py`:
  `test_amount_is_formatted_not_a_bare_number` **failed** with
  `'₹1,234.50' not in 'Expense — 1234.50\n…'`. Restored the line; full suite back
  to 61 passed; `git status` clean. So the guard fails for the reason it exists —
  a bare amount on the card is caught, not just asserted about.
- **callback_data contract holds end-to-end.** `CONFIRM`/`CANCEL` = `"confirm"`/
  `"cancel"`. `dispatch()` (`kanakko/app.py:56-65`) copies `callback_query.data`
  verbatim into `ButtonPress.data`, so the string round-trips to the handlers
  (tasks 56/57) unchanged. Neither collides with the category buttons'
  `cat:<name>` prefix (`kanakko/categories.py:49`).
- **Exercised the function directly** (not just via the tests): a normal expense
  renders `'Expense — ₹1,234.50\nCategory: Food\nDate: 2026-08-06\nNote: lunch'`
  with buttons `['confirm', 'cancel']`. Amount is grouped, ₹-prefixed, two
  decimals — the §9 display form. `type.capitalize()` is safe because
  `Transaction.type` is `Literal["expense","income"]`, always lowercase.
- **Plain text, no `parse_mode` — a correct safety choice.** The note is the
  user's own wording; rendering it as plain text means a note containing `_`,
  `*`, `[`, or `<` needs no Markdown/HTML escaping and can't inject formatting.
- **Spec fit.** §4 (a confirm card on every transaction) and the Phase-1 build
  order are honoured: the card carries only Confirm/Cancel here. §5's "category
  buttons directly on the confirm card" is task 63 (Phase 2), correctly deferred,
  not a Phase-1 omission. Nothing falsely ticked — task 55 asked for exactly this
  render, and the tests are real, not a value-returning stub.

### Low-severity note (non-blocking)

- **`kanakko/confirm.py:26` — a null `category` renders `Category: None`.** The
  docstring says a null category "routes to the category buttons instead (§3,
  task 59), so a card always has a category to name," but the function has no
  precondition guard. Exercised directly, a `category=None` transaction renders
  `'Income — ₹50,000.00\nCategory: None\nDate: …'`. This is not wrong *today* —
  task 59 (the routing that keeps null-category txns away from this renderer) is
  still unchecked, so "missing is not wrong." But the failure mode is silent: if
  task 59's routing is later buggy or a new caller forgets it, the user sees the
  literal string `None` rather than the function refusing. A one-line
  `assert txn.category is not None, "null category must route to buttons (§3, task 59)"`
  would make the stated precondition fail loud instead of leaking `None` to the
  card. Optional; the real fix lives in task 59.

---

## 2026-08-06 — `811391f` — reclassify webhook-origin task to `[human]` (§14, task 42)

**Scope:** Docs-only. `TASKS.md` reclassifies the open "Verify the webhook's
origin" task to `[human]` and records a blocker: its first step is authoring a
new `docs/DECISIONS.md` line (env key name, whether the secret is required in
Phase 1) plus a `setWebhook` reconfiguration carrying a `secret_token`. No code,
schema, or test changed.

**Status: ✅ DONE** — no blocking issues.

### What I checked

- **Diff is docs-only.** `git show HEAD --stat` → `TASKS.md | 19 +++---`, one
  file. Nothing under `kanakko/`, `migrations/`, or `tests/` touched, so none of
  the money / timezone / soft-delete / category silent-wrongness classes are in
  scope.
- **Suite still green.** `uv run pytest -q` → **57 passed, 1 warning** (the
  pre-existing Starlette/httpx deprecation). A docs-only change shouldn't move
  this, and it didn't.
- **The spec really is silent on webhook origin.** `grep -ni
  'secret_token|secret-token|X-Telegram|webhook|origin|no auth'
  docs/DECISIONS.md` plus reading §1 (line 11, "No auth" — user-facing), §13
  (lines 255–265, Mini App `initData`), §14 (lines 291–305, webhook-over-polling
  only), and the deferred table (lines 320–345). No `secret_token`/origin
  decision exists anywhere, and it is not on the deferred list. The commit's
  central factual claim holds.
- **The `[human]` classification is justified.** The queue's own convention
  (TASKS.md lines 9–11): `[human]` = touches credentials, deployment, or an
  external account. This task's first step is authoring a new DECISIONS decision
  — `CLAUDE.md` forbids the loop from editing the spec — and its wiring requires
  `setWebhook` with a secret (a credentials/deployment action). Both halves are
  genuinely off-limits to the loop. Correctly left `- [ ]` (open), not ticked, so
  this is a deliberate skip, not a false tick.
- **The safety gate is sound.** Confirmed the current `/webhook`
  (`kanakko/app.py:69–91`) only classifies via `dispatch()` and `log.info`s — it
  writes **no rows** (verified by reading the handler and `grep -rn
  'pending_transactions|INSERT INTO pending' kanakko/` → no matches; the
  confirm-card and Handle-Confirm tasks are unimplemented). So "a forged update
  can't forge anything yet" is true today, and gating origin auth ahead of the
  first ledger write (Handle Confirm) is the right, conservative call.

### Advisory (non-blocking)

- **`TASKS.md:53` — "render confirm card writes no rows" is a claim about an
  unimplemented task, not a verified fact.** In the intended flow (parse → store
  a `pending_transactions` row → render the card), the pending row is created
  *before* the card is shown — and lines 56/64 ("clear the pending row",
  "update the pending row") confirm a pending row is expected to exist by then.
  If whoever implements the confirm-card task persists the pending row there,
  then a forged update could write `pending_transactions` rows before origin auth
  lands. This is low-consequence — `pending_transactions` is a staging table, not
  the `transactions` ledger, and the material forgery risk (a real transaction)
  is still the Handle-Confirm write the gate correctly targets — but the
  implementer of the confirm-card task should confirm whether it persists a
  pending row and, if so, treat that as the true earliest write point rather than
  trusting the "writes no rows" phrasing. No change required to this commit.

---

## 2026-08-06 — `4402e63` — `/webhook` endpoint + update dispatch (§14, task 41)

**Scope:** `kanakko/app.py` gains `TextMessage`/`ButtonPress` frozen dataclasses,
a `dispatch()` that classifies a Telegram update into one of those or `None`, and
a `POST /webhook` that parses the body, routes via `dispatch()`, and returns
`{"ok": true}` (200) unconditionally. New `tests/test_webhook.py` (6 checks).
TASKS.md ticks task 41 and adds a webhook-origin-auth task.

**Status: ✅ DONE** — no blocking issues.

### What I checked

- **Full suite.** `uv run pytest -q` → **57 passed, 1 warning** (the warning is a
  pre-existing Starlette/httpx deprecation, unrelated). Matches the commit
  message's claim of 57.
- **Webhook tests.** `uv run pytest tests/test_webhook.py -q` → **6 passed**.
- **The load-bearing guard actually guards.** Claim: without the `try/except`
  around `request.json()`, a malformed body would 500 and Telegram would
  redeliver forever. I reproduced a copy of the endpoint *without* the guard and
  posted `b"not json"` with a JSON content-type: it returned **500** (verified,
  not assumed). With the guard in place the endpoint returns 200. So the guard
  fails for the reason it exists — this is not a surface-form assertion.
- **Spec fit (§14).** `docs/DECISIONS.md` §14 (lines 291–305) decides webhook over
  long polling and is otherwise silent on dispatch shape; there is no routing
  detail to contradict. Nothing in the diff touches money, timezones, categories,
  soft-delete, or SQL, so none of the silent-wrongness classes apply here — no
  `float`, no `transactions` read, no UTC bucketing.
- **`dispatch()` classification.** Text → `TextMessage`; inline-button tap →
  `ButtonPress`; photo (no `text`), `edited_message`, `channel_post`, and `{}` →
  `None`. Confirmed by reading the code and the three-case ignore test. `message`
  and `callback_query` are read with `or {}`, so a `null` value doesn't crash.
- **Deferred webhook auth.** The endpoint is public and does no origin check. This
  is deliberately deferred with a new TASKS.md line (Telegram `secret_token` →
  `X-Telegram-Bot-Api-Secret-Token`), to be decided in DECISIONS before handlers
  write rows. Correct call per CLAUDE.md ("Adding [a decision] is a decision"):
  since `dispatch()` only logs and writes nothing, a forged update currently has
  no effect, so there is no present data risk. Not a finding — it's sequenced
  ahead of the handlers that would make it matter.

### Findings

None blocking.

**Note (non-blocking, low):** `dispatch()` reads `chat.get("id")` and
`message.get("message_id")` with `.get()`, so a text update missing its `chat`
yields `TextMessage(chat_id=None, …)` rather than being ignored — the dataclass
fields are typed `int` but can be `None`. Telegram always includes `chat` on a
`message`, and no handler consumes these yet, so this is future-facing: the
handler tasks (42–46) should reject an action with a `None` `chat_id` rather than
try to reply to it. Recording it here so it isn't lost, not asking for a change now.

---

## 2026-08-06 — `7852921` — category nullable, amount non-nullable in parse schema (§3, task 40)

**Scope:** `parse_schema()` makes `category` nullable (`type: ["string","null"]`,
`enum: [*schema_enum(), None]`); `Transaction.category` becomes `str | None` and
`_category_is_known` passes `None` through while still rejecting any non-null
value outside the closed set. New test `test_null_category_validates_without_retry`.

**Status: ✅ DONE** — no blocking issues.

### What I checked

- **Spec fit (§3).** `docs/DECISIONS.md` §3 lines 56–70: uncertainty is a
  nullable field, `category: null` → show buttons, `amount` **not** nullable, no
  confidence score, and the nullability is enforced by the schema rather than
  validation code. The diff matches all of it: null is added in `parse_schema()`
  (not in `categories.py`), `amount` type stays `"string"` and required, and no
  confidence field appears anywhere. `schema_enum()` still returns only real
  categories (`kanakko/categories.py:35`), so null is not smuggled in as a
  category (§11 preserved).
- **Validator behaviour.** `parse.py:142-147` — `None` short-circuits before the
  membership test, so a null category validates; any non-null string outside
  `ALL_CATEGORIES` still raises. `amount` remains routed through
  `parse_amount` (§9), untouched.
- **`uv run pytest -q` → 51 passed** (matches the commit message).
- **Revert-verified the new test guards the behaviour, not the spelling.** I
  temporarily restored the old `if value not in ALL_CATEGORIES:` and re-ran
  `tests/test_parse.py`: `test_null_category_validates_without_retry` went red
  with `IndexError: tuple index out of range` (rejected null → unwanted retry →
  the single-response `_feed` runs dry), exactly as the commit claims. Restored;
  `git diff` clean. So the test fails for the reason it exists.
- `TASKS.md` box for task 40 is now ticked and the work behind it is real, not a
  stub.

No `float` on the money path, no read-through-view concern (parse only), no
timezone bucketing, no secret, no new dependency. Nothing to change.

---

## 2026-08-06 — `cbc3a9d` — validate parse result with Pydantic + one retry (§2, task 39)

**Scope:** Adds a `Transaction` Pydantic model and `parse_message()` to
`kanakko/parse.py`, which validates the model's JSON and retries exactly once on
a schema failure. 6 new tests in `tests/test_parse.py`; `TASKS.md` line 39 ticked.
Diff +162/-8 across three files, no production code beyond `parse.py`.

**Status: ✅ DONE** — no blocking issues. The commit does what §2 asks (one
Pydantic validation + one retry), keeps `amount` on the `Decimal` rail through
the single §9 door, and correctly defers nullable-category to task 40. I
reproduced both defects the new tests exist to catch and confirmed they go red.

### What I checked (and what it returned)

- **`git show HEAD`** — confirmed scope: `parse.py`, `test_parse.py`, `TASKS.md` only.
- **`uv run pytest`** → `50 passed, 1 warning`. **`uv run pytest tests/test_parse.py -v`**
  → all 12 pass (6 new).
- **Float coercion probe** (`/tmp/probe.py`, direct `Transaction.model_validate`):
  `amount: 500.5` (JSON float) → **rejected**; `amount: "500.50"` → `Decimal('500.50')`;
  `amount: 500` (JSON int) → `Decimal('500.00')` (exact, no paise lost);
  `date: "not-a-date"` → **rejected**. The §9 leak is closed: a provider that
  ignores strict mode and returns a bare number can't launder a lost-precision
  float into a `Decimal`.
- **Revert-verified the two guards actually bite:**
  - Replaced the retry's `except` body with `raise` → `test_schema_failure_is_retried_exactly_once`,
    `test_two_failures_raise_and_do_not_loop`, `test_float_amount_is_refused_not_coerced`,
    `test_non_json_content_is_retried` all **failed** (4 red). Restored → green.
  - Made `_amount_is_exact` return `value` unchanged (bypassing `parse_amount`, letting
    Pydantic coerce float→Decimal) → `test_float_amount_is_refused_not_coerced` and
    `test_two_failures_raise_and_do_not_loop` **failed** (2 red). Restored → 12 green.
  Both commit-message revert claims hold.
- **Spec fit:** §2 caveat (strict mode best-effort per provider → one Pydantic
  validation + single retry) is implemented literally. No confidence score
  (§3) — `Transaction` has no such field. `category` stays required, matching the
  commit's own note that nullability is task 40; not a finding since that task is
  unchecked. `extra="forbid"` mirrors the schema's `additionalProperties: false`.
- **Retry classification** matches the docstring: `ValidationError`/`json.JSONDecodeError`
  retried; `httpx.HTTPError` and a missing key (`KeyError`/`IndexError` from
  `response.json()["choices"][0]...`) propagate un-retried
  (`test_http_error_is_not_retried` confirms the HTTP case, 1 call, no retry).

### Findings

None blocking. Two non-issues noted for the record:

- **Int amount accepted despite the schema declaring `amount` a string.** A JSON
  integer (`500`) decodes to `int`, which `parse_amount` accepts exactly →
  `Decimal('500.00')`. No paise are lost (an int has no fractional part), so this
  is harmless resilience, not a §9 violation. Only `float` loses precision, and
  that path is rejected. No change needed.
- **`response.json()` raising `JSONDecodeError` is treated as a schema failure.**
  If OpenRouter's *outer* HTTP body (not just the model `content`) is malformed,
  `call()` raises `JSONDecodeError` before reaching `json.loads(content)`, so it
  gets retried once like a bad-content failure rather than propagating as a
  transport error. Retrying once is harmless and rare; not worth a guard.

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
