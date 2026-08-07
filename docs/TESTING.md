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

---

## 1. Entry and the confirm loop

The core path — §2, §3, §4. If this is broken, nothing else matters.

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 1.1 | Send `spent 250 on lunch` | A confirm card appears within a few seconds: amount ₹250.00, type expense, category Food, today's date, the note | ⬜ | |
| 1.2 | Tap **Confirm** | Toast says "Saved ✅"; the card stops offering Confirm | ⬜ | |
| 1.3 | Send `got 50000 salary` | Confirm card: ₹50,000.00, **income**, Salary | ⬜ | |
| 1.4 | Confirm it | Saved | ⬜ | |
| 1.5 | Send `paid 1200 to bigbasket yesterday` | Card shows **yesterday's** date, not today, and category Groceries | ⬜ | |
| 1.6 | Send `spent 99.50 on coffee` | Card shows **₹99.50** — paise preserved exactly, not ₹99 or ₹100 | ⬜ | |
| 1.7 | Send `hello how are you` | A rephrase prompt. **No card, no amount invented.** | ⬜ | |
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

---

## 6. Access control and households

**Phase 8 — not built yet.** Leave ⛔ until it ships; listed now so the plan is
testable the day it lands.

### 6a. Signup gate

| # | Step | Expected | Status | Notes |
|---|---|---|---|---|
| 6a.1 | From a Telegram account that has never used the bot, send `/start` (invite mode) | Polite refusal. No ledger created | ⬜ | |
| 6a.2 | After 6a.1, *(operator)* check the database | **No user row** was created for that person | ⬜ | |
| 6a.3 | Same account, open a valid signup invite link | Welcome message; a household of one is created | ⬜ | |
| 6a.4 | Open the **same** link again from a third account | Refused — codes are single-use | ⬜ | |
| 6a.5 | Open an expired code | Refused, and says so distinctly from "already used" | ⬜ | |
| 6a.6 | Send `/start garbage_payload` | Refused cleanly; no half-created household | ⬜ | |

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

## Summary

| Section | Pass | Fail | Blocked |
|---|---|---|---|
| 1. Entry and confirm | | | |
| 2. Corrections | | | |
| 3. Dashboard | | | |
| 4. Reminders | | | |
| 5. Failure handling | | | |
| 6. Households *(not built)* | | | |
| 7. Security | | | |
| 8. Recovery | | | |

**Anything ❌ on the money path (1.6, 2.4, 3.6, 8.3, 8.4) stops a release.** The
rest is judgement.
