"""Recurring-rule cron send — the confirm card for a due auto-debit (§18).

§18: "On the day, the cron sends the ordinary confirm card... with Confirm /
Change amount / Skip. Not a silent insert." `confirm_card`'s `cancel_label` and
`change_amount_button` give the rule the wording and the third button §18
names, and every tap routes through the existing `handle_confirm`/
`handle_cancel`/`handle_change_amount_request` webhook code, unchanged.

The `cron` service runs `python -m kanakko.jobs.recurring` daily, `Asia/Kolkata`
(crontab in `cron/kanakko.crontab`).
"""

import logging
import time

from kanakko import configure_logging
from kanakko.confirm import SKIP_LABEL, confirm_card
from kanakko.db import connect, due_rules_today, list_accounts, save_pending
from kanakko.eventlog import ERROR, OK, log_event, ms_since
from kanakko.jobs import DeliveryFailures
from kanakko.jobs.evening import today_ist
from kanakko.parse import Transaction
from kanakko.tg import send_message

log = logging.getLogger(__name__)


def run(conn) -> int:
    """Send the confirm card for every rule due today; return how many were sent.

    Its own fan-out shape, not `jobs.fan_out`: that helper's `deliver(user_id,
    telegram_user_id)` is one row per *user*, but a household can have several
    rules due the same day, each needing its own card, its own pending row and
    its own amount/category/account — data that signature has no room for. The
    same two safety properties still hold: each rule sends in its own
    savepoint, so one bad recipient (a blocked bot, a stale chat) does not stop
    the rest and does not roll back sends that already succeeded; failures are
    collected and raised together, after a commit, so the job still exits
    non-zero without silencing anyone ahead of the failure.
    """
    day = today_ist()
    start = time.perf_counter()
    considered = sent = 0
    failures: list[tuple[int, Exception]] = []
    for rule in due_rules_today(conn, day.day):
        considered += 1
        try:
            with conn.transaction():
                txn = Transaction(
                    type="expense",
                    amount=rule["amount"],
                    category=rule["category"],
                    date=day,
                    note=f"Recurring: {rule['category']}",
                    account=rule["account_name"],
                )
                accounts = list_accounts(conn, rule["created_by"])
                text, keyboard = confirm_card(
                    txn, accounts, cancel_label=SKIP_LABEL, change_amount_button=True
                )
                response = send_message(rule["telegram_user_id"], text, keyboard)
                card_message_id = response["result"]["message_id"]
                save_pending(
                    conn, rule["created_by"], card_message_id, txn,
                    recurring_rule_id=rule["rule_id"],
                )
                sent += 1
        except Exception as exc:  # noqa: BLE001 — one rule must not stop the rest
            failures.append((rule["telegram_user_id"], exc))
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
        log_event("job.recurring", status=ERROR, **fields)
        raise DeliveryFailures(sent, failures)
    log_event("job.recurring", status=OK, **fields)
    return sent


def main() -> None:
    """Cron entry point: `python -m kanakko.jobs.recurring`."""
    configure_logging()
    with connect() as conn:
        sent = run(conn)
    log.info("recurring cron sent %d confirm card(s)", sent)


if __name__ == "__main__":
    main()
