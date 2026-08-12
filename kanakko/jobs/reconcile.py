"""Reconciliation nudge — the weekly "what does your bank say?" (§18).

§18: "Weekly, per account... A different figure writes an adjustment row
against the external account." This sends the nudge and marks it awaiting a
reply; `kanakko.reconcile_flow.handle_reconcile_reply` is the other half, fired
from the webhook once the user answers. The balance quoted in the nudge is
`db.accounts.account_balances`'s own figure, read once per household and reused
across that household's accounts — not a third copy of the balance formula
(`db.reconcile.create_adjustment` already carries the reviewed second one).

The `cron` service runs `python -m kanakko.jobs.reconcile` weekly, `Asia/Kolkata`
(crontab in `cron/kanakko.crontab`).
"""

import logging
import time
from decimal import Decimal

from kanakko import configure_logging
from kanakko.db import account_balances, accounts_for_reconcile, connect, create_reconcile_ask
from kanakko.eventlog import ERROR, OK, log_event, ms_since
from kanakko.jobs import DeliveryFailures
from kanakko.money import format_amount
from kanakko.tg import send_message

log = logging.getLogger(__name__)


def nudge_text(name: str, kind: str, balance: Decimal) -> str:
    """§18's example nudge, worded per kind — a credit account is asked what is
    *owed*, never what is "in" it, the same distinction onboarding draws."""
    if kind == "credit":
        return f"I think you owe {format_amount(balance)} on {name} — what does your card statement say?"
    return f"I think your {name} has {format_amount(balance)} — what does your bank say?"


def run(conn) -> int:
    """Send the reconcile nudge for every household's own accounts; return how many were sent.

    Its own fan-out loop, the same shape `jobs.recurring.run` uses rather than
    `jobs.fan_out`: a household can own several accounts, each needing its own
    nudge and its own awaiting-reply row — data `fan_out`'s per-user signature has
    no room for. The same two safety properties still hold: each account sends in
    its own savepoint, so one bad recipient does not stop or roll back the rest;
    failures are collected and raised together, after a commit, so the job still
    exits non-zero without silencing anyone ahead of the failure.
    """
    start = time.perf_counter()
    considered = sent = 0
    failures: list[tuple[int, Exception]] = []
    balances_by_household: dict[int, dict[int, Decimal]] = {}
    for account in accounts_for_reconcile(conn):
        considered += 1
        try:
            with conn.transaction():
                household_id = account["household_id"]
                if household_id not in balances_by_household:
                    balances_by_household[household_id] = {
                        row["account_id"]: row["balance"]
                        for row in account_balances(conn, account["owner"])
                    }
                balance = balances_by_household[household_id][account["account_id"]]
                text = nudge_text(account["name"], account["kind"], balance)
                response = send_message(account["telegram_user_id"], text)
                nudge_message_id = response["result"]["message_id"]
                create_reconcile_ask(
                    conn, account["owner"], nudge_message_id, account["account_id"]
                )
                sent += 1
        except Exception as exc:  # noqa: BLE001 — one account must not stop the rest
            failures.append((account["telegram_user_id"], exc))
    fields = dict(
        source="cron",
        considered=considered,
        delivered=sent,
        failed=len(failures),
        duration_ms=ms_since(start),
    )
    if failures:
        # Commit before raising: the caller's `with connect()` would otherwise
        # roll back every successful send on the way out.
        conn.commit()
        log_event("job.reconcile", status=ERROR, **fields)
        raise DeliveryFailures(sent, failures)
    log_event("job.reconcile", status=OK, **fields)
    return sent


def main() -> None:
    """Cron entry point: `python -m kanakko.jobs.reconcile`."""
    configure_logging()
    with connect() as conn:
        sent = run(conn)
    log.info("reconcile cron sent %d nudge(s)", sent)


if __name__ == "__main__":
    main()
