# Kanakko — what it is, and who it's for

*A first read before the demo. No technical knowledge assumed.*

---

## The one-line version

**Kanakko is a personal finance tracker that lives inside Telegram.** You tell it
what you spent the way you'd text a friend — "spent 500 on groceries" — and it
does the rest.

There is no app to download, no account to create, no forms to fill in.

---

## The problem

Almost everyone has tried to track their money. Almost everyone has stopped.

The reason is nearly always the same: **logging an expense takes too long.** Open
the app, wait for it to load, tap "Add", choose a category from a list, type the
amount, pick a date, hit save. Twenty seconds, five taps — for a ₹40 chai. Do
that four times a day and you quit within a fortnight.

The apps that solve the speed problem solve it by connecting to your bank, which
means handing over credentials, tolerating messy auto-imported labels, and still
correcting everything by hand.

So people fall back on a notes app or a spreadsheet, which are fast to write and
useless to read. At the end of the month you have a list, not an answer.

**Kanakko's bet is that the fastest interface anyone already has is the chat app
they use all day.** Typing "spent 500 on groceries" takes three seconds, and one
tap confirms it. That's it. There is nothing to open, because Telegram is already
open.

---

## How it actually works

The whole product, in thirty seconds:

1. You message the bot: **"spent 450 on dinner"**
2. It replies with a small card: *Expense — ₹450.00, Food, today* and two buttons.
3. You tap **Confirm**. Done.

If it guessed the category wrong, the right one is a single tap on that same
card. If you typed something it can't read, it says so and asks again — it never
invents a number.

It understands ordinary human phrasing, not a fixed syntax: *"paid 1200 to
bigbasket yesterday"*, *"got 50000 salary"*, *"swiped 2000 on dinner"*, *"put
5000 in SIP"*. It handles Indian money vocabulary natively — lakh, UPI, SIP, FD,
chit fund, RD.

For anything you want to *look at* rather than type, there's a dashboard that
opens inside Telegram itself — no browser, no login.

---

## What it does today

Everything below is built and running.

### Logging money without friction
- **Plain-English entry.** Type it how you'd say it.
- **One-tap confirmation** on every entry, so speed never costs accuracy.
- **Instant correction** — change the category on the card before you confirm.
- **`/undo`** removes your last entry.
- **Full editing** of anything older, in the dashboard: amount, date, note,
  category, which account it came from.
- **Delete with undo**, so a mis-tap costs nothing.

### Seeing where the money went
- A **dashboard inside Telegram**: this week, this month, or all time.
- **One headline number** — what you've spent — because that's the question
  people actually have.
- **Category breakdown** with bars, biggest first.
- **Month-on-month comparison**, so you can see whether it's getting better.
- **Recent transactions**, tap any one to edit it.

### Money that isn't spending
This is the part most trackers get wrong, and it's the part Indian users feel
most.

When you move ₹5,000 into an SIP, an FD, or a chit fund, that money hasn't been
*spent* — but it has left your bank. Most apps force you to call it an expense,
which makes your spending look wildly inflated and hides your savings entirely.

Kanakko treats it as what it is: **money that moved, not money that went.**

- **Separate accounts** — bank, cash, credit card, and savings pots (SIP, FD,
  chit, RD, or money you lent someone).
- **Transfers** between them, kept out of your spending figures.
- **Credit cards handled properly.** A card swipe is spending; paying the card
  bill is not. Counting both — which simpler tools do — doubles your spending.
- **Money that comes back.** An FD maturing or a chit paying out is your own
  money returning, not income. Logged as income, one payout would make the month
  look like a windfall and wreck every average.
- **Balances per account**, so "how much do I actually have?" has an answer.

### Sharing money with people
- **Households.** Invite your partner or family; you share one ledger and see the
  same totals.
- **Everyone logs their own** entries, and can only edit or remove their own.
- **Ownership and membership** are managed in chat — invite, remove, leave, hand
  over ownership.
- Anyone who wants their money kept private simply gets their own household.

### Staying honest over time
- **A gentle nudge at noon** — but only on days you've logged nothing. No noise.
- **A summary every evening at 9pm** with the day's total.
- **A monthly report on the 1st** — income, spending, top categories.
- **Recurring payments.** For an SIP or EMI that debits automatically and you'd
  never remember to log, Kanakko asks on the day: *"SIP ₹5,000 today?"* — Confirm,
  change the amount, or skip. It never adds money to your ledger without you
  saying yes.
- **A weekly reconciliation check** — *"I think your bank has ₹42,300. What does
  your bank say?"* Any difference is corrected with a visible entry. This is what
  stops the slow drift that kills every manual tracker.

### Refunds
Returned something? A refund is recorded against the **original purchase** and can
be partial. It reduces that category's spending rather than counting as income,
and it can never exceed what you actually paid.

---

## Who this is for

**People who have already failed at expense apps.** The largest group. They know
they should track spending, they've tried three apps, and they lasted a fortnight
each. The pitch to them is friction, not features.

**Couples and families sharing money.** Two people, one household budget,
constant "did you pay for that?" A shared ledger both people can write to from
their own phones removes the reconciliation conversation entirely.

**Anyone with Indian savings instruments.** Chit funds, SIPs, RDs, fixed
deposits, and money lent to relatives are enormous in real Indian household
finance and almost invisible in international finance apps. Kanakko was designed
around them rather than having them bolted on.

**Cash-heavy households.** People who withdraw and spend physical cash are badly
served by anything that depends on a bank feed.

**People who won't connect a bank account.** Some for privacy, some because they
tried and the labels were a mess. Kanakko never asks for a bank login.

**Anyone already living in Telegram.** No new app, no new habit, no new icon on
the home screen.

---

## What it deliberately does not do

Worth knowing before anyone writes a claim we can't back.

- **It does not connect to your bank.** You tell it what you spent. That's the
  trade for having no credentials involved, and the weekly reconcile check is
  what keeps the numbers honest.
- **It does not track what your investments are worth.** It knows what you put
  into an SIP and what came back out. Live market value needs price feeds and is
  a different product — your broker already does it, and doing it badly is worse
  than not doing it.
- **It is not a budgeting app** — no envelopes, no limits, no alerts when you
  overspend. It answers "where did it go", not "you may not".
- **It is not for business accounting.** No GST, no invoices, no clients.
- **It requires Telegram.** That's the whole delivery mechanism.

**What it stores about a person:** their Telegram id, their first name (so a
household roster can say who is who), and the money they log. No email, no phone
number, no bank credentials, no contacts. `/delete_account` erases all of it.

---

## Where it's going

Nothing here is committed to a date — treat it as direction, not roadmap.

- **Getting your data out** — export to a spreadsheet.
- **Reading bank statements** so a month can be reconciled in one go instead of
  entry by entry. Statement formats differ by bank and this is a real piece of
  work, so it waits until the weekly reconcile proves insufficient.
- **Smoother setup for opening balances**, so the very first "how much do I have"
  is right on day one.
- **Wallets** (Paytm, Amazon Pay balances) as their own account type, if enough
  people keep money in them.

Two things are open questions the team can help answer: **who exactly we launch
to first**, and **what the strongest single sentence of positioning is** — speed,
sharing, or savings visibility. Those are three different products in one, and
the answer decides the campaign.

---

## For the demo

What's worth watching for:

1. **How fast an entry is.** Type, tap, done. Time it.
2. **The moment it gets a category right** without being told.
3. **The credit-card double-count** — a swipe, then paying the bill, and the
   spending figure moving only once.
4. **An SIP contribution** leaving the bank without appearing as spending.
5. **The shared household** — two phones, one set of numbers.

Currently **invite-only**, deliberately: it's a real ledger with real money in it,
and access is by a single-use link.
