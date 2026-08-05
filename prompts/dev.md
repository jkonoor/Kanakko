You are implementing Kanakko. This is one iteration of a loop. You have no
memory of previous iterations — the repository is your memory.

## Orient

Read, in this order:

1. `AGENTS.md` — commands and conventions
2. `TASKS.md` — the queue
3. `docs/DECISIONS.md` — **the specification.** It is the authority.
4. `docs/PLAN.md` — only the phase your task belongs to

Read only what the task needs. Context you spend orienting is context you don't
have for the work.

## Pick exactly one task

Take the **first unchecked `- [ ]` task in `TASKS.md` that is not marked
`[human]`**. Skip `[human]` tasks entirely — they touch credentials or
deployment and are done by a person.

If there is no such task, print `QUEUE EMPTY` and stop without changing
anything.

Do one task. Not two. If the task turns out to contain two separable pieces,
split it in `TASKS.md`, do the first, and leave the second unchecked.

## Before you write anything

**Search the codebase first. Do not assume something isn't implemented.**
Grep for the function, the route, the table, the constant. A previous iteration
may have built it, or built most of it. Rebuilding what exists is the most
common way this loop wastes a cycle and creates duplicate, diverging code.

Check whether the thing you're about to add already exists under a different
name before you add it.

## While you write

**No placeholders. No stubs. No `TODO`. No `pass  # implement later`. No
simplified version "for now".** Implement the task completely, the way it will
ship. An unfinished implementation that is checked off is worse than an
unchecked task, because the next iteration will believe it is done.

If you genuinely cannot finish the task — a decision is missing, a credential
is needed, the spec is ambiguous — then:

- leave the box unchecked,
- append a line to *Found by QA* in `TASKS.md` stating precisely what is
  blocking,
- commit nothing else,
- and stop.

Follow the conventions in `AGENTS.md` — they are not suggestions. Especially:
`Decimal` for money, `timestamptz` in UTC with `Asia/Kolkata` bucketing, reads
via `active_transactions`, categories only from `kanakko/categories.py`, no new
dependencies without a reason.

## Never

- **Never edit `docs/DECISIONS.md`.** If the spec is wrong or contradicts
  itself, add a task saying so. A spec edited to match the code has stopped
  being a spec.
- **Never deploy, never call the Dokploy API, never touch a live database.**
  Deployment is manual. The deployment credentials are not in your environment
  and their absence is deliberate.
- **Never commit a secret.** Not in code, not in a compose file, not in a test
  fixture.
- Never edit a migration that has already been applied. Add a new one.
- Never mark a `[human]` task done.

## Finish

1. Run `uv run pytest`. If your change broke something, fix it in this same
   iteration — a red test committed is a trap for the next one.
2. Tick the box in `TASKS.md`.
3. Commit. One iteration is one commit. The message says what was done and
   why, and names the task.

Then stop. Do not start the next task.
