"""Scheduled jobs — one module per job, run by the `cron` service (§12).

The three jobs share one shape: read every user, do a per-user read, send them a
message, record it in `reminder_log`. `fan_out` is that shape, with the failure
handling all three need.
"""

import time
from typing import Callable, Iterable

import psycopg

from kanakko.eventlog import ERROR, OK, log_event, ms_since


class DeliveryFailures(Exception):
    """One or more users could not be delivered to, after the rest succeeded.

    Raised at the *end* of a fan-out, never in the middle, so a single bad
    recipient cannot silence everybody else's summary.
    """

    def __init__(self, sent: int, failures: list[tuple[int, Exception]]):
        self.sent = sent
        self.failures = failures
        detail = "; ".join(
            f"{tg_id}: {type(exc).__name__}: {exc}" for tg_id, exc in failures
        )
        super().__init__(f"delivered to {sent}, failed for {len(failures)} — {detail}")


def fan_out(
    conn: psycopg.Connection,
    users: Iterable[tuple[int, int]],
    deliver: Callable[[int, int], bool],
    *,
    job: str,
) -> int:
    """Run `deliver(user_id, telegram_user_id)` per user, isolating failures.

    Returns how many were actually delivered to — `deliver` returns False for a
    user it deliberately skipped (the noon nudge suppresses active users), which
    is not a failure.

    **One `job.<name>` event per run, not per user** (§17). The fan-out is the
    shared shape all three jobs run through, so this is the one place the counts
    exist — considered, delivered, skipped, failed — without putting the whole
    ledger's shape on the volume. `source="cron"`. A run with failures logs
    `status="error"` and still raises `DeliveryFailures` (the job exits non-zero,
    the line is *in addition* to that, never instead of it — §12).

    Two properties the jobs need and did not have:

    **One bad recipient does not stop the rest.** Previously a single
    `send_message` raise aborted the whole loop, so a user who blocked the bot
    silenced everyone after them in the list.

    **Work already done is kept.** Each user runs in its own savepoint
    (`conn.transaction()` nests when already in a transaction), so a failure
    rolls back only that user's partial writes. Everyone else's `reminder_log`
    row survives — which matters because the noon nudge reads those rows to find
    its suppression boundary, so a lost row silently widens the next window.

    Failures are collected and raised together at the end, after an explicit
    commit that preserves the successful work. The job still exits non-zero, so
    a failure is never silent — deliberately an exception rather than a log line,
    since logging is being designed separately.
    """
    start = time.perf_counter()
    considered = sent = skipped = 0
    failures: list[tuple[int, Exception]] = []
    for user_id, telegram_user_id in users:
        considered += 1
        try:
            with conn.transaction():
                if deliver(user_id, telegram_user_id):
                    sent += 1
                else:
                    skipped += 1
        except Exception as exc:  # noqa: BLE001 — one user must not stop the rest
            failures.append((telegram_user_id, exc))
    fields = dict(
        source="cron",
        considered=considered,
        delivered=sent,
        skipped=skipped,
        failed=len(failures),
        duration_ms=ms_since(start),
    )
    if failures:
        # Commit before raising: the caller's `with connect()` would otherwise
        # roll back every successful user on the way out.
        conn.commit()
        log_event(f"job.{job}", status=ERROR, **fields)
        raise DeliveryFailures(sent, failures)
    log_event(f"job.{job}", status=OK, **fields)
    return sent
