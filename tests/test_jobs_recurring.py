"""The recurring-rule cron send (§18) — who gets asked, and what survives the tap.

`run` finds today's due rules, sends each its own confirm card, and threads the
rule id through `save_pending` so a later Confirm can tie the transaction back
to it (tested end to end here, not just at the `db` layer, since the send and
the pending write happen in the same job). The failure-isolation property
mirrors `jobs.evening`'s: one bad recipient must not stop or roll back the rest.
"""

import pytest
from conftest import default_account_of, household_of

from kanakko import eventlog
from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.db import (
    confirm_pending,
    create_recurring_rule,
    get_or_create_user,
    set_recurring_rule_active,
)
from kanakko.jobs import DeliveryFailures, recurring
from kanakko.jobs.evening import today_ist
from kanakko.migrate import migrate
from kanakko.money import parse_amount


def _rule(conn, telegram_user_id, category, amount, day, *, active=True):
    """A user, its household-of-one, and one recurring rule in it. Returns
    `(user_id, household_id, rule)`."""
    user_id = get_or_create_user(conn, telegram_user_id)
    hh = household_of(conn, user_id)
    acc = default_account_of(conn, hh)
    rule = create_recurring_rule(conn, user_id, acc, category, parse_amount(amount), day)
    if not active:
        set_recurring_rule_active(conn, user_id, rule["rule_id"], False)
    return user_id, hh, rule


def _wipe_household(conn, telegram_user_id, household_id):
    """Undo `_rule`'s inserts after a test that had to commit (FK-ordered)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_transactions WHERE user_id ="
                     " (SELECT user_id FROM users WHERE telegram_user_id = %s)", (telegram_user_id,))
        cur.execute("DELETE FROM recurring_rules WHERE household_id = %s", (household_id,))
        cur.execute("DELETE FROM household_members WHERE household_id = %s", (household_id,))
        cur.execute("DELETE FROM accounts WHERE household_id = %s", (household_id,))
        cur.execute("DELETE FROM households WHERE household_id = %s", (household_id,))
        cur.execute("DELETE FROM users WHERE telegram_user_id = %s", (telegram_user_id,))


def test_run_sends_a_confirm_card_and_links_the_pending_row_to_the_rule(conn, monkeypatch):
    """A due rule gets its own card: Confirm / Change amount / Skip, not Cancel (§18)."""
    migrate(conn)
    day = today_ist()
    user_id, _hh, rule = _rule(conn, 810100, EXPENSE_CATEGORIES[0], "5000", day.day)

    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent["chat_id"] = chat_id
        sent["text"] = text
        sent["reply_markup"] = reply_markup
        return {"result": {"message_id": 4242}}

    monkeypatch.setattr(recurring, "send_message", fake_send)

    count = recurring.run(conn)

    assert count == 1
    assert sent["chat_id"] == 810100
    assert "5,000.00" in sent["text"]
    labels = [b.text for row in sent["reply_markup"].inline_keyboard for b in row]
    assert "⏭️ Skip" in labels
    assert "✏️ Change amount" in labels
    assert "❌ Cancel" not in labels

    with conn.cursor() as cur:
        cur.execute(
            "SELECT recurring_rule_id FROM pending_transactions WHERE telegram_message_id = 4242"
        )
        assert cur.fetchone() == (rule["rule_id"],)

    # Confirming the sent card ties the settled row back to the rule (§18).
    row = confirm_pending(conn, user_id, 4242, source="cron", update_id=None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT recurring_rule_id FROM transactions WHERE txn_id = %s", (row["txn_id"],)
        )
        assert cur.fetchone() == (rule["rule_id"],)
    conn.rollback()


def test_run_skips_a_rule_not_due_today_and_a_paused_one(conn, monkeypatch):
    migrate(conn)
    day = today_ist()
    other_day = 1 if day.day != 1 else 2
    _rule(conn, 810200, EXPENSE_CATEGORIES[0], "100", other_day)
    _rule(conn, 810201, EXPENSE_CATEGORIES[0], "100", day.day, active=False)

    monkeypatch.setattr(recurring, "send_message", lambda *a, **k: pytest.fail("must not send"))

    assert recurring.run(conn) == 0
    conn.rollback()


def test_one_blocked_recipient_does_not_silence_the_others(conn, monkeypatch):
    """Mirrors `jobs.evening`'s failure-isolation guard, for recurring's own loop.

    Two rules due the same day, the first recipient's send raising. The second
    must still be sent and its pending row must still exist after the first's
    failure — which only holds because each rule runs in its own savepoint and
    the job commits before raising.
    """
    migrate(conn)
    conn.commit()  # baseline the test's own rows survive the job's commit
    day = today_ist()
    _, blocked_hh, blocked_rule = _rule(conn, 810300, EXPENSE_CATEGORIES[0], "100", day.day)
    _, ok_hh, ok_rule = _rule(conn, 810301, EXPENSE_CATEGORIES[0], "200", day.day)

    def flaky_send(chat_id, text, reply_markup=None):
        if chat_id == 810300:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return {"result": {"message_id": 5000 + chat_id}}

    monkeypatch.setattr(recurring, "send_message", flaky_send)

    events = []
    eventlog.bind_sink(events.append)
    try:
        with pytest.raises(DeliveryFailures) as caught:
            recurring.run(conn)
    finally:
        eventlog.unbind_sink()

    assert caught.value.sent == 1
    assert [tg for tg, _ in caught.value.failures] == [810300]

    assert len(events) == 1
    assert events[0]["event"] == "job.recurring"
    assert events[0]["status"] == "error"
    assert events[0]["considered"] == 2
    assert events[0]["delivered"] == 1
    assert events[0]["failed"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT recurring_rule_id FROM pending_transactions WHERE telegram_message_id = %s",
            (5000 + 810301,),
        )
        assert cur.fetchone() == (ok_rule["rule_id"],)
        cur.execute(
            "SELECT count(*) FROM pending_transactions WHERE recurring_rule_id = %s",
            (blocked_rule["rule_id"],),
        )
        assert cur.fetchone() == (0,)  # the failed send wrote no pending row

    _wipe_household(conn, 810300, blocked_hh)  # this test committed, so clean up after itself
    _wipe_household(conn, 810301, ok_hh)
    conn.commit()
