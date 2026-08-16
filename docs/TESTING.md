# Manual test plan

End-to-end checks a **person** runs against a real deployment, in Telegram, on a
phone. This is not the automated suite — `uv run pytest` covers what a headless
run can see. This file covers what it cannot: whether the thing actually works
when a human uses it.

## How to use this

Work top to bottom within a section; later steps in a section often depend on
earlier ones. Fill in **Status** and **Notes** as you go.

| Status | Meaning |
|---|---|
| ⬜ | Not run |
| ✅ | Passed as described |
| ❌ | Failed — put what actually happened in Notes |
| ⚠️ | Worked, but something was off (slow, ugly, confusing) |
| ⛔ | Blocked — couldn't run it, say why |

**A ❌ or ⚠️ needs the actual behaviour written down, not "didn't work".** What you
sent, what came back, and roughly when — the timestamp is what makes a container
log searchable afterwards.

**Before starting**, record what you are testing:

| Field | Value |
|---|---|
| Date / time (IST) | |
| Tester | |
| Deployed commit | |
| Bot | @KanakkoBot |
| Dashboard URL | |

> **These tests write real data to the real ledger.** There is no staging
> environment. Prefer distinctive amounts (₹1.11, ₹2.22) so test rows are easy to
> find and remove afterwards, and clean up with `/undo` or the dashboard when done.

> **Trace mode is on by default, so everything you type here lands on disk** —
> the raw message, the prompt built from it, and the model's reply, kept until
> rotation (§17). That is deliberate for a beta among friends and is what makes a
> parse bug diagnosable, but assume a test session is recorded verbatim. Section
> 5a.12 is where you look at what was kept.

---

## 1. Entry and the confirm loop

The core path — §2, §3, §4. If this is broken, nothing else matters.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 1.1 | Send `spent 250 on lunch` | A confirm card appears within a few seconds: amount ₹250.00, type expense, category Food, today's date, the note | ⬜ | |
| 1.2 | Tap **Confirm** | Toast says "Saved ✅"; the card settles into a "✅ Saved" receipt and stops offering Confirm/Cancel | ⬜ | |
| 1.3 | Send `got 50000 salary` | Confirm card: ₹50,000.00, **income**, Salary | ⬜ | |
| 1.4 | Confirm it | Saved | ⬜ | |
| 1.5 | Send `paid 1200 to bigbasket yesterday` | Card shows **yesterday's** date, not today, and category Groceries | ⬜ | |
| 1.6 | Send `spent 99.50 on coffee` | Card shows **₹99.50** — paise preserved exactly, not ₹99 or ₹100 | ⬜ | |
| 1.7 | Send `hello how are you` | A rephrase prompt. **No card, no amount invented.** | ⬜ | |
| 1.7a | Send `hi` (or `/help`) | The help text — how to log an expense. Answered instantly, **no parser call**; it does not count against your daily message cap | ⬜ | |
| 1.7b | Send `spent five hundred on lunch` | Still reaches the parser (a card or category buttons) — an exact-greeting short-circuit must never swallow a real entry with no digits | ⬜ | |
| 1.8 | Send `bought something for 300` | Category buttons instead of a confirm card (the model couldn't tell) | ⬜ | |
| 1.9 | Tap a category on that card | Card re-renders as a full confirm card with your category and gains Confirm/Cancel | ⬜ | |

**1.6 is the money check.** Anything other than ₹99.50 means paise are being lost
somewhere, and that is a stop-everything bug.

---

## 2. Corrections

§5 — the reason there is no field editor.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 2.1 | Send `spent 111 on tea`, then tap **Cancel** | Toast "Discarded ❌", and **the card disappears from the chat entirely** | ⬜ | |
| 2.2 | Scroll up — is the cancelled card gone? | Yes. No leftover card with dead buttons | ⬜ | |
| 2.2a | Confirm an entry, then tap **Cancel** on that same (now settled) card | Toast "Already gone"; **the receipt stays in the chat** and the entry stays in the ledger — a stale Cancel never wipes a saved receipt | ⬜ | |
| 2.3 | Confirm a new entry, then send `/undo` | Reply names exactly what was removed (type, amount, category) | ⬜ | |
| 2.4 | Send `/undo` again immediately | "Nothing to undo." — it does **not** delete a second, older transaction | ⬜ | |
| 2.5 | On a confirm card, tap a *different* category | Card re-renders with the new category; amount and date unchanged | ⬜ | |

**2.4 is the dangerous one.** A second `/undo` silently eating an older real
transaction is the kind of bug that only shows up as a wrong total weeks later.

---

## 3. The Mini App dashboard

§13. Open it from the bot's menu button.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 3.1 | Tap the menu button | Dashboard opens; briefly shows "Loading…", then figures | ⬜ | |
| 3.2 | Check the top | A **Week / Month / All** switcher, then one large "Spent" figure | ⬜ | |
| 3.3 | Tap **Week** | Figures change to this week's; the category bars change with them | ⬜ | |
| 3.4 | Tap **All** | All-time figures; **no delta line** (there is no prior period) | ⬜ | |
| 3.5 | Back on **Month**, read the delta | Either `▲/▼ N% vs last month`, or "no comparison yet" if this is your first month. **Never a percentage against zero** | ⬜ | |
| 3.6 | Check Balance and Income | Balance = Income − Spent, to the paise. Income is the only coloured figure | ⬜ | |
| 3.7 | Check the category bars | Sorted biggest first, each with an amount **and a percentage** | ⬜ | |
| 3.8 | Check the Recent list | Rows readable; every ✕ lines up in one column down the page | ⬜ | |
| 3.9 | Find a row with a note | The note sits **below** its row, not overlapping the date or amount | ⬜ | |

### 3a. Dashboard interactions

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 3a.1 | Change a row's category via its dropdown | List refreshes; the new category sticks | ⬜ | |
| 3a.2 | Check the bars after 3a.1 | The breakdown reflects the change | ⬜ | |
| 3a.3 | Tap ✕ on a test row | Row disappears; totals drop by that amount | ⬜ | |
| 3a.4 | Tap the ✕ with your thumb, not a fingernail | Comfortably hittable; you don't hit the dropdown by mistake | ⬜ | |
| 3a.5 | **Minimize** the dashboard, log a new expense in the chat, restore the dashboard | Figures include the new entry **without** closing and reopening | ⬜ | |

**3a.5 is a regression check** — this was broken once and fixed. It depends on
your Telegram build emitting the right event, so it is worth re-running after any
Telegram update.

### 3b. Themes

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 3b.1 | Set Telegram to **dark** theme, open the dashboard | Dark background, light text. No glaring white panel | ⬜ | |
| 3b.2 | In dark theme, look at an income row | The income amount is legible — not a dark green on near-black | ⬜ | |
| 3b.3 | Switch Telegram to **light**, reopen | Light background, dark text, everything still legible | ⬜ | |
| 3b.4 | In either theme, read the percentages and notes | Grey text is readable, not washed out | ⬜ | |

### 3c. The row editor: amount, date, account (task 1704)

The row rebuild replaced three always-visible action glyphs with one collapsed
row — date, category, amount, note — that expands into an editor on tap. These
rows test that editor directly, not the pre-rebuild shape 3a.3/3a.4 above still
describe.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 3c.1 | Tap any row in Recent | Row expands into a panel: **Amount, Category, Date**, then **Account** (only if a second account exists), then **Note**, then a rule and **Delete**/**Refund** as labelled buttons — not glyphs | ⬜ | |
| 3c.2 | Change the Amount field to a different value | The dashboard's totals reload to match the new amount, and the panel you were editing **closes back to the collapsed row** — every field edit reloads the whole list from the server, so it doesn't stay open for a second edit | ⬜ | |
| 3c.3 | Re-expand that row, change the Date field, then re-expand it once more | The Date field now shows your new date, and the `date-human` span beside it (e.g. "06 Aug 2026") spells out the same date | ⬜ | |
| 3c.4 | With a second account on the household, re-expand a non-transfer row and change its Account field | The dashboard's per-account balances (the Accounts panel, §18) shift accordingly | ⬜ | |
| 3c.5 | Open the panel on a `transfer` row | No Category field, no Account field (a transfer names two ends, not one) — only Amount, Date, Note | ⬜ | |
| 3c.6 | Tap **Delete** inside a row's panel | Row disappears immediately; a toast reads "Deleted ₹N.NN · Undo" for a few seconds | ⬜ | |
| 3c.7 | Tap **Undo** on that toast before it clears; on a separate delete, let the toast sit instead | Undo restores the row and totals; left alone, the toast **auto-dismisses** on its own | ⬜ | |
| 3c.8 | Turn the device's network off, then edit any field (e.g. Amount) | A "Couldn't save — try again." toast appears. `shell.py`'s `mutate()` always reloads after a write, success or failure — offline, that reload fails too, so the **whole** app area turns into "Could not load dashboard.", not just the one field reverting | ⬜ | |
| 3c.9 | Restore the network, reopen the dashboard | The field you tried to edit in 3c.8 shows its **original**, pre-edit value — nothing was silently saved while offline | ⬜ | |

**3c.1 and 3c.5 are the field-set checks** — a `transfer` row must never offer a
Category or Account control, since it has neither (§18). **3c.2 is easy to
misread as a bug**: the panel closing after every edit is `mutate()`'s
always-reload design working as intended, not a lost edit — the value did save;
re-expand to see it. **3c.8–3c.9 are the one no automated check can cover.**

---

## 4. Scheduled reminders

§12. These fire on a clock, so they cannot be tested on demand — note the date you
observed each one.

| # | What | When (IST) | Expected | Status | Notes |
|---|---|---|---|---|---|
| 4.1 | Noon nudge | 12:00 daily | Arrives **only if you logged nothing** since the previous evening summary | ⬜ | |
| 4.2 | Noon nudge suppression | 12:00, on a day you already logged something | **No message.** A nudge here is a bug | ⬜ | |
| 4.3 | Evening summary | 21:00 daily | Always arrives, with the day's total and entry count | ⬜ | |
| 4.4 | Evening summary on an empty day | 21:00 | Still arrives, saying nothing was logged | ⬜ | |
| 4.5 | Monthly report | 09:00 on the 1st | Previous month's income, expenses, balance, top categories | ⬜ | |

**If a reminder never arrives**, that is the cron sidecar, not the bot — the
`kanakko-cron` container's log is where the answer is. Note the date, because a
missed 12:00 cannot be retried until tomorrow.

---

## 5. Failure handling

§6 (Phase 6). These check the bot **explains itself** instead of going silent.
Some need an operator to break something deliberately.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 5.1 | *(operator)* Set an invalid `OPENROUTER_API_KEY`, send a message | A message saying the parser can't be reached and your message wasn't saved. **Not silence** | ⬜ | |
| 5.2 | After 5.1, check the chat | No confirm card, no stored transaction | ⬜ | |
| 5.3 | *(operator)* Restore the key, send a message | Works normally again, with no leftover queue of retried messages arriving at once | ⬜ | |
| 5.4 | *(operator)* Check the container log during 5.1 | A `WARNING` line with the status code and provider message. **The API key must not appear in it** | ⬜ | |
| 5.5 | Send 3–4 messages rapidly | Each gets its own card; none is lost or duplicated | ⬜ | |

### 5a. Logging, the audit trail, and trace mode

§17 (Phase 8). Almost all operator checks — they need a shell in the
`kanakko-web` container and `psql` on `kanakko-db`. The operational log is
`$LOG_DIR/events.jsonl` (JSON Lines, rotated by stdlib at 5 MB × 5); the audit
trail is the `transaction_events` table; trace artefacts are
`$LOG_DIR/trace/<update_id>/`.

**Before starting**, log one distinctive expense (₹3.33) and confirm it — most rows
below refer back to it. Note its `update_id` from the log.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 5a.1 | `tail -3 $LOG_DIR/events.jsonl` after confirming it | A `transaction.confirmed` line, `"status":"ok"`, with `txn_id`, `amount`, `duration_ms` and `source":"webhook"` | ⬜ | |
| 5a.2 | Compare that line's `update_id` with the `pending.created` line before it | **Different** ids — the card and the tap are two updates. The confirm's id must match the tap, not the typing | ⬜ | |
| 5a.3 | Tap **Confirm** a second time on the same card | A second line with `"status":"noop"` — **not** `ok` — and no second ledger row | ⬜ | |
| 5a.4 | `jq -r 'select(.status=="error")' $LOG_DIR/events.jsonl` | Empty on a normal day. Anything here is a handler that raised, and it should correspond to a 500 | ⬜ | |
| 5a.5 | `grep -c "$TELEGRAM_BOT_TOKEN" $LOG_DIR/events.jsonl` and the same for the OpenRouter key | **0 for both**, and repeat it against `$LOG_DIR/trace/` | ⬜ | |
| 5a.6 | `select action, before, after, source, update_id from transaction_events where txn_id = <yours>` | One row: `action='confirm'`, `before` **NULL**, `after` carrying the amount, `source='webhook'`, `update_id` set | ⬜ | |
| 5a.7 | Change that row's category in the dashboard, re-run 5a.6 | A second row, `action='recategorise'`, `before`/`after` showing **both** categories, `source='miniapp'`, `update_id` **NULL** | ⬜ | |
| 5a.8 | `/undo` a fresh entry, then query its events | `action='undo'`, `after` NULL. Every money change has exactly one row | ⬜ | |
| 5a.9 | `select count(*) from transactions t where not exists (select 1 from transaction_events e where e.txn_id = t.txn_id)` | **0** — a ledger row with no audit row means the two came apart, which is the failure §17 exists to prevent | ⬜ | |
| 5a.10 | `ls $LOG_DIR/trace/<update_id>/` for a successful parse | `01__input.json`, `02__request.json`, `03__parse__ok.json` — the listing alone tells you it succeeded | ⬜ | |
| 5a.11 | Send `hello how are you`, then `ls` that update's folder | `03__parse__invalid.json` — the **filename** names the failure, no file opened | ⬜ | |
| 5a.12 | Open `01__input.json` and `02__request.json` from any folder | Your raw text and the full prompt, in clear. **Expected** — this is the trade §17 records for revisiting before real customers | ⬜ | |
| 5a.13 | *(operator)* Set `TRACE_KEEP=3`, send 5 messages, `ls $LOG_DIR/trace/ \| wc -l` | **3.** Rotation that never fires is the bug that fills the shared volume | ⬜ | |
| 5a.14 | *(operator)* Set `TRACE_MODE=off`, send a message | No new folder appears, and the bot still works normally | ⬜ | |
| 5a.15 | *(operator)* Restart `kanakko-web`, then check `events.jsonl` | **Still there, with the old lines.** This is the volume mount — without it every deploy wipes the evidence | ⬜ | |
| 5a.16 | *(operator)* In `kanakko-cron`, write a file into `$LOG_DIR` and read it back from `kanakko-web` | The **same** volume is mounted on both — §17 requires it, and the jobs are the reason | ⬜ | |
| 5a.17 | *(operator)* Run `python -m kanakko.jobs.evening` in `kanakko-cron`, then `jq 'select(.event\|startswith("job."))' $LOG_DIR/events.jsonl` | **One** line per run — `event":"job.evening"`, `"status":"ok"`, `"source":"cron"`, with `considered`, `delivered`, `skipped`, `failed` and `duration_ms`. One event per *run*, not per user | ⬜ | |
| 5a.18 | *(operator)* Force a delivery failure (a blocked recipient), re-run the job | A `"status":"error"` line **and** the job exits non-zero (`echo $?` ≠ 0) — the log line is *in addition* to the raise, never instead of it. Earlier users' `reminder_log` rows are still present | ⬜ | |
| 5a.19 | Change the ₹3.33 row's Amount in the dashboard, then its Date, re-running 5a.6's query after each | **Two** more rows, same shape as 5a.7's `recategorise`: `action='edit'`, `source='miniapp'`, `update_id` NULL — one with `before='{"amount": ...}'`/`after='{"amount": ...}'`, the next with `before='{"occurred_on": ...}'`/`after='{"occurred_on": ...}'` (migration 013's `edit` action, task 974) | ⬜ | |
| 5a.20 | If a second account exists, also change the row's Account field, re-run the query | A third `edit` row: `before='{"account_id": <old id>}'`/`after='{"account_id": <new id>}'` | ⬜ | |

**5a.5 and 5a.9 are the two that matter.** A token in the log is a leak that
survives on disk until rotation, and an orphan ledger row means the audit write is
no longer inside the money transaction — the exact regression the arrangement in
`db.py` exists to make impossible.

**5a.15–5a.18 cannot pass until the volume is mounted** on both applications
(the `[human]` task in Phase 8) — the job rows need the `kanakko-cron` container
writing to the shared volume. Until then, mark them ⛔ rather than ❌ — nothing is
broken, the infrastructure just isn't there yet.

---

## 6. Access control and households

**Phase 9 shipped 2026-08-08** — these are live checks now, not a forward plan.
Every row needs a **second Telegram account** you control; 6b onwards needs a
third for the "other member" cases.

### 6a. Signup gate

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6a.1 | From a Telegram account that has never used the bot, send `/start` (invite mode) | Polite refusal. No ledger created | ⬜ | |
| 6a.2 | After 6a.1, *(operator)* check the database | **No user row** was created for that person | ⬜ | |
| 6a.7 | From an account that has **never** used the bot, open the chat and tap the **menu button** to open the Mini App — do *not* press Start | The dashboard shows **no data** and the request is refused (403). This is the door the bot's own gate doesn't cover | ⬜ | |
| 6a.8 | After 6a.7, *(operator)* `select count(*) from users where telegram_user_id = <that account>` | **0.** Opening the Mini App must mint nothing | ⬜ | |
| 6a.9 | After 6a.7, from that same account send `spent 100 on tea` in the chat | Still the invite-only refusal. Opening the dashboard must not have made them known to the bot | ⬜ | |
| 6a.10 | Admit that account properly (invite link), then open the Mini App again | Dashboard loads normally | ⬜ | |
| 6a.3 | Same account, open a valid signup invite link | Welcome message; a household of one is created | ⬜ | |
| 6a.4 | Open the **same** link again from a third account | Refused — codes are single-use | ⬜ | |
| 6a.5 | Open an expired code | Refused, and says so distinctly from "already used" | ⬜ | |
| 6a.6 | Send `/start garbage_payload` | Refused cleanly; no half-created household | ⬜ | |

### 6a-i. Issuing a signup invite (`/invite_signup`)

Needs `ADMIN_TELEGRAM_IDS` set to your own Telegram id on `kanakko-web`. **Run
6a-i.1 before setting it** — the refusal is the point of the feature, and once
the variable is set you cannot see it again from your own account.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6a-i.1 | *(before setting the env var)* Send `/invite_signup ravi` | Refused — "only the operator". **No link, and no row in `invites`** | ⬜ | |
| 6a-i.2 | Set `ADMIN_TELEGRAM_IDS` to your id, redeploy, send `/invite_signup ravi` | A single-use link `https://t.me/<bot>?start=s-…` | ⬜ | |
| 6a-i.3 | Send bare `/invite_signup` | Asks for a label. **No row in `invites`** | ⬜ | |
| 6a-i.4 | From a second account **that has never used the bot**, open the 6a-i.2 link | Welcome; that account can log an expense immediately | ⬜ | |
| 6a-i.5 | From that second account, send `/household` | **Only them.** They are in their own household, not yours — the whole point of the signup kind | ⬜ | |
| 6a-i.6 | From a third account, open the **same** link | Refused as already used | ⬜ | |
| 6a-i.7 | From the second account (admitted, not an admin), send `/invite_signup priya` | Refused. An admitted tester must not be able to admit more people | ⬜ | |
| 6a-i.8 | *(operator)* `select code, kind, household_id, label, used_by from invites` | The `s-…` row is `kind = 'signup'` with `household_id` NULL; `/invite` rows are `household` with an id | ⬜ | |
| 6a-i.9 | Log an expense from your own account and from the second account, then compare each `/household` and dashboard | Neither sees the other's money | ⬜ | |
| 6a-i.10 | Send `/help` | `/invite_signup` is **not** listed — it is operator-only, and the "/" menu is the user manual | ⬜ | |

6a-i.7 is the guard that matters most: the gate is `ADMIN_TELEGRAM_IDS`, not "owns
a household". If it ever reverts to the ownership check `/invite` uses, every
tester becomes an admitter and the closed beta opens itself one account at a time.

**6a.7–6a.9 are a real defect that was found and fixed before the merge, so
re-run them after any change to the Mini App routes.** The routes resolved their
caller with `get_or_create_user`, which *created* a row for whoever opened the
dashboard. That row then satisfied the bot's gate — which asks "does a user row
exist?" — so the Mini App let anyone past the invite gate. It also left them in no
household, and a Confirm then hit a NOT NULL violation on `household_id`, 500ing
into a Telegram redelivery loop. A user row is now the credential: it exists only
after `/start` admitted the person, so resolving must never create one.

### 6b. Household membership

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6b.1 | As owner, send `/invite` | A `t.me/…?start=…` link comes back | ⬜ | |
| 6b.2 | Second person opens it | They join **your** household, not a new one | ⬜ | |
| 6b.3 | Second person logs an expense | It appears in **your** dashboard totals | ⬜ | |
| 6b.4 | Send `/household` | Both members listed, owner marked | ⬜ | |
| 6b.5 | Second person sends `/undo` right after **you** logged something | It removes **their** last entry, not yours | ⬜ | |
| 6b.6 | Second person tries to delete **your** row in the dashboard | Refused — you may only remove what you entered | ⬜ | |
| 6b.7 | At 12:00, when only *one* member logged something | The member who logged **nothing** still gets nudged | ⬜ | |
| 6b.8 | At 21:00 | Both members get the **household** total, not personal totals | ⬜ | |

**6b.5 and 6b.6 are the rules most easily got wrong** — both are about acting on
someone else's entry.

### 6c. Leaving, removal, deletion

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6c.1 | Second member removes **themselves** | Allowed. They end up in a fresh household of one | ⬜ | |
| 6c.2 | Owner tries to remove themselves while still owner | **Refused** — transfer ownership first | ⬜ | |
| 6c.3 | Owner transfers ownership, then leaves | Allowed; household still has an owner | ⬜ | |
| 6c.4 | Remove a member and choose **retain** | Household totals **unchanged**; their entries stay | ⬜ | |
| 6c.5 | Remove a member and choose **delete** | A warning first, saying past reports will stop reconciling | ⬜ | |
| 6c.6 | Confirm the deletion, then re-open last month | The total is **lower than the summary you were sent at the time** | ⬜ | |

**6c.6 is expected, not a bug** — it is the accepted price of real deletion (§16),
and the warning in 6c.5 must have said so. If 6c.5 warned only "this cannot be
undone", **that is a finding**.

### 6d. Rate limit

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6d.1 | *(operator)* Set the daily cap to 3, send 4 messages | The 4th is refused with an explanation | ⬜ | |
| 6d.2 | *(operator)* Confirm no OpenRouter call was made for the 4th | The cap exists to stop spend, so it must refuse **before** the LLM call | ⬜ | |
| 6d.3 | After IST midnight, send again | Allowed — the count rolls over at **IST** midnight, not UTC | ⬜ | |

---

## 7. Security

Mostly operator checks. These should be re-run after any deployment change.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 7.1 | `curl -X POST <domain>/webhook -d '{}'` with no secret header | **403** | ⬜ | |
| 7.2 | `curl <domain>/app/data` with no auth | **401** | ⬜ | |
| 7.3 | `curl <domain>/healthz` | 200, with a version | ⬜ | |
| 7.4 | Open the dashboard URL in a normal browser (not Telegram) | Loads the shell but shows no data — it cannot authenticate | ⬜ | |
| 7.5 | Log an expense with a note containing `<script>alert(1)</script>`, view the dashboard | The text appears **literally**. No popup, no missing text | ⬜ | |
| 7.6 | *(operator)* `grep -r "sk-or-v1" .` in the repo | No matches — secrets live in the environment only | ⬜ | |

**7.5 is a real attack**, not a formality: the note is user-typed and rendered
into the page, so this is the check that stored XSS hasn't been reintroduced.

---

## 8. Recovery

Phase 5. Untested to date — **there is real financial data and no verified
restore.**

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 8.1 | Trigger a manual database backup | Completes without error | ⬜ | |
| 8.2 | Restore it into a scratch database | Restores cleanly | ⬜ | |
| 8.3 | Count rows in the restored copy | Matches the live count at backup time | ⬜ | |
| 8.4 | Spot-check amounts in the restore | Still `NUMERIC(12,2)` — paise intact, no floats | ⬜ | |

**Until 8.1–8.4 have been ✅ at least once, the ledger is one bad day from being
gone.** Everything above tests whether the app works; this tests whether the data
survives.

---

## 9. Accounts, transfers, and reconciliation

Phase 10, §18. Needs at least one household with a second Telegram-visible
signal you can check off-app (a real bank balance figure works fine — the
reconcile checks below only need *a* number to compare against, not a real
account).

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 9.1 | With only the default account, send `spent 40 on tea`, confirm | **One tap, same as §1** — no Account line on the card, no extra button. Accounts only become visible once a second one exists | ⬜ | |
| 9.2 | Send `/account credit 0` | A credit card account is created, owing ₹0 | ⬜ | |
| 9.3 | Send `spent 40 on tea` again | Card now shows an **Account: Bank** line with a button to change it | ⬜ | |
| 9.4 | Send `swiped 2000 on dinner`, confirm | Card's account is the credit card, not Bank; category Food | ⬜ | |
| 9.5 | Send `paid the credit card bill 2000`, confirm | Card shows **Bank → \<card name\>**, no category line, no category buttons | ⬜ | |
| 9.6 | Note the spending total after 9.3, then open the dashboard and check this month's spending after 9.4–9.5 | Rises by **exactly ₹2,000** across 9.4–9.5 (the swipe) — the bill payment in 9.5 adds **nothing**, not a second ₹2,000 | ⬜ | |
| 9.7 | Send `put 5000 in FD`, confirm | Card asks `New savings account "FD"?` over Confirm/Cancel; confirming creates the `locked` account and moves ₹5,000 out of Bank | ⬜ | |
| 9.8 | Check this month's spending again | **Unchanged** by 9.7 — a contribution to a locked account is not spending | ⬜ | |
| 9.9 | Send `FD matured 5500`, confirm | A transfer FD → Bank, not income | ⬜ | |
| 9.10 | Check this month's income | **Unchanged** by 9.9 | ⬜ | |
| 9.11 | Send `/account FD` | Reports what went in and came back — "put in ₹5,000.00, got back ₹5,500.00" | ⬜ | |
| 9.12 | Log an expense, e.g. `spent 500 on shoes`, confirm; then send `refund 500` | A candidate list; tapping the shoes row records a **partial or full** refund | ⬜ | |
| 9.13 | Refund the shoes row for ₹200, then send `refund 500` again and try to refund **more than ₹300 remains** | Refused — the sum of refunds against one transaction can never exceed it | ⬜ | |
| 9.14 | Check the category total for 9.12–9.13 | Reduced by the refunded amount only, never turned into income | ⬜ | |
| 9.15 | *(operator)* Run `python -m kanakko.jobs.reconcile` in `kanakko-cron` | Each of your accounts (except `external`) gets its own message quoting the app's current figure and asking what the real one is | ⬜ | |
| 9.16 | Reply to that message with a **different** figure | A visible adjustment transaction appears (dashboard, `/undo` names it), against the `external` account — never a silent rewrite | ⬜ | |
| 9.17 | *(operator)* Run the reconcile job again, reply with the **same** figure the app now shows | "No changes needed" — no new row written | ⬜ | |
| 9.18 | In a household with two accounts, reply to a reconcile nudge **without** using Telegram's Reply function | Falls through to an ordinary parse (or a rephrase prompt) rather than misfiling as an adjustment on the wrong account | ⬜ | |

**9.6 is the money check for this section** — it is the double-count §18 exists
to prevent, the same weight 1.6 and 2.4 carry above; check the *rise* across
9.4–9.5, not an absolute total, since 9.1/9.3 already added tea spending
earlier in the flow. **9.13 is the second one**:
a refund exceeding what remains is a silent over-refund if it is ever allowed
through. **9.1 is the regression check**: every account feature in this section
must leave the single-account daily path exactly as it was.

**Run 9.19 and 9.20 before 9.2** — they need the household still on its one
default account, which 9.2 permanently ends by creating a credit account. If
you already ran 9.2+ here, use a second, never-onboarded household instead.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 9.19 | Still single-account (before 9.2), open any row in the dashboard's editor | **No Account field at all** — not a dropdown with one dead option. `recent.py`'s `_txn_panel` only renders it when `len(accounts) > 1` | ⬜ | |
| 9.20 | Still single-account, send `paid the credit card bill 2000` | **Record what actually happens.** The risk noted 2026-08-16 was the model minting a `locked` account named "Credit card" for this — per `parse.py`, that specific misfile can't happen here: `new_locked_account`, `transfer` and the account vocabulary guidance are all gated on `len(accounts) > 1` (`parse_schema`, `build_request`), so a single-account household's prompt carries none of them and the model can only return a plain `expense`/`income`. What it actually guesses (type, category) for a bill payment with no card account yet is untested — if it inflates spending or does anything else surprising, that is a finding for `REVIEWS.md`, not a silent fix | ⬜ | |
| 9.21 | Send `/account credit 5000` (updates the account 9.2 created) | Reply: "Got it — Card (credit), you owe ₹5,000.00." — the onboarding ask is phrased as what you **owe**, never what you have | ⬜ | |
| 9.22 | Send `/account bank 2000` (or your default account's name) | Reply: "Got it — Bank (spending), you have ₹2,000.00."; the dashboard's **Accounts** panel shows Bank's balance shifted by the same ₹2,000 | ⬜ | |

---

## 10. Recurring rules

§18 (Phase 10), migrations 015-017. A recurring rule never writes silently —
the cron always sends the ordinary confirm card and asks, because a chit or
SIP amount can vary month to month and a silently-wrong row is worse than a
missing one. `/recurring` only *creates* a rule; pausing and deleting are the
dashboard's job.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 10.1 | Send `/recurring` with no arguments | A usage hint (`/recurring 5000 5 Bills & Utilities Bank`) — no card, no rule created | ⬜ | |
| 10.2 | Send `/recurring 5000 5 Bills & Utilities Bank` | Reply: "Got it — ₹5,000.00 for Bills & Utilities from Bank on day 5 of the month. Manage it from Dashboard." | ⬜ | |
| 10.3 | Send `/recurring 5000 35 Food Bank` (day out of range) | Refused — day of month must be 1-31, never silently clamped to the 1st or last day | ⬜ | |
| 10.4 | Send `/recurring 5000 5 NotACategory Bank` | Refused — category has to be one of the expense categories | ⬜ | |
| 10.5 | Send `/recurring 5000 5 Food NotAnAccount` | Refused — account has to be one of yours | ⬜ | |
| 10.6 | With a `locked` account already open (e.g. the FD from §9), send `/recurring 500 5 <category> FD` | Rule is created — a locked account is a valid recurring target (e.g. a SIP), not rejected as spendable-only | ⬜ | |
| 10.7 | *(operator)* Pick a rule due today (or create one for today's day-of-month), run `python -m kanakko.jobs.recurring` in `kanakko-cron` | The user gets the ordinary confirm card, but with **✅ Confirm / ✏️ Change amount / ⏭️ Skip** — no plain Cancel button | ⬜ | |
| 10.8 | Tap **✅ Confirm** on that card | Saved like any other entry — appears in the dashboard and `/undo` names it the same as a manually-typed transaction | ⬜ | |
| 10.9 | *(operator)* After 10.8, check that transaction's `recurring_rule_id` | Set to the rule's id — the link that ties an auto-debit back to its rule | ⬜ | |
| 10.10 | On a due card, tap **✏️ Change amount** | Bot sends a **new message** asking to reply with just the number — it does not turn into an inline field on the card itself | ⬜ | |
| 10.11 | Reply with a number | Card settles at the new amount, not the rule's original amount | ⬜ | |
| 10.12 | Reply with something that isn't a number | "I couldn't read that as an amount…" and it asks again — the pending card is not silently dropped | ⬜ | |
| 10.13 | On a due card, tap **⏭️ Skip** | Discarded like a Cancel; **no transaction is written** for that month | ⬜ | |
| 10.14 | In the dashboard, find the rule and tap **Pause** (⏸) | Row dims; *(operator)* re-run the job on the rule's due day | **No card is sent** while paused | ⬜ | |
| 10.15 | Tap **Resume** (▶) on the same rule, then *(operator)* re-run the job on its next due day | Card is sent again, normally | ⬜ | |
| 10.16 | Delete (✕) a rule that has already produced at least one confirmed transaction | Rule disappears immediately — **no confirm dialog, no undo toast** (unlike a transaction row delete); *(operator)* check that past transaction | Transaction still exists in the ledger, unaffected — only its link to the rule is cleared | ⬜ | |
| 10.17 | *(operator)* Leave a due card unresolved, then re-run the job for the same rule (same day, or its next monthly due date without ever having acted on the first card) | **Untested, no guard found in code** — the job does not check whether a card for this rule/month is already pending, so a second card may be sent. Record what actually happens; a duplicate send is a real finding for `REVIEWS.md` | ⬜ | |

**10.9 is the audit check** — a settled recurring transaction with no
`recurring_rule_id` breaks the provenance link, the same class of gap §17's
audit trail exists to catch elsewhere. **10.13 and 10.16 are the money
checks**: a skip must leave the ledger untouched, and a rule delete must never
touch a past transaction, only detach it.

---

## Summary

| Section | Pass | Fail | Blocked |
|---|---|---|---|
| 1. Entry and confirm | | | |
| 2. Corrections | | | |
| 3. Dashboard | | | |
| 4. Reminders | | | |
| 5. Failure handling | | | |
| 5a. Logging and audit trail | | | |
| 6. Access control and households | | | |
| 7. Security | | | |
| 8. Recovery | | | |
| 9. Accounts, transfers, reconciliation | | | |
| 10. Recurring rules | | | |

**Anything ❌ on the money path (1.6, 2.4, 3.6, 8.3, 8.4, 9.6, 9.13, 10.13,
10.16) stops a release.** The rest is judgement.
