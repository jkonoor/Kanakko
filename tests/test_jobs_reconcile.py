"""The weekly reconcile-nudge cron send (§18) — who gets asked, and how.

`run` sends every household's own live, non-`external` accounts a "what does
your bank say?" nudge and marks each awaiting a reply. The balance quoted comes
from `db.account_balances` — not a third copy of the balance formula — and the
failure-isolation property mirrors `jobs.recurring`'s: one bad recipient must
not stop or roll back the rest.
"""

import pytest
from conftest import household_of

from kanakko import eventlog
from kanakko.db import pending_awaiting_reconcile
from kanakko.jobs import DeliveryFailures, reconcile
from kanakko.migrate import migrate


def _account(conn, hh, uid, *, kind, name, opening="0"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name, opening_balance)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING account_id",
            (hh, uid, kind, name, opening),
        )
        return cur.fetchone()[0]


def test_run_nudges_every_live_non_external_account(conn, monkeypatch):
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (820100) RETURNING user_id")
        (uid,) = cur.fetchone()
    hh = household_of(conn, uid)
    _account(conn, hh, uid, kind="credit", name="Card", opening="-500")

    sent = []

    def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return {"result": {"message_id": 4000 + len(sent)}}

    monkeypatch.setattr(reconcile, "send_message", fake_send)

    count = reconcile.run(conn)

    # The default 'Bank' (spending) account plus the seeded 'Card' (credit) —
    # `external` is never nudged.
    assert count == 2
    assert {chat_id for chat_id, _ in sent} == {820100}
    texts = " ".join(text for _, text in sent)
    assert "what does your bank say" in texts
    assert "what does your card statement say" in texts
    assert "₹500.00" in texts  # the card's derived balance, what is owed
    # §18's normal case is a multi-account household — pending_awaiting_reconcile
    # only guesses the account when exactly one ask is outstanding, so the nudge
    # itself has to steer the user into replying rather than typing bare.
    assert texts.count("Reply to this message with the number.") == 2

    # Two accounts means two outstanding asks — `pending_awaiting_reconcile`
    # with no `reply_to_message_id` only resolves a single outstanding ask
    # (see its docstring), so the "were both marked awaiting" check reads the
    # rows directly rather than through that ambiguous-on-purpose lookup.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pending_transactions"
            " WHERE user_id = %s AND awaiting_reconcile_account_id IS NOT NULL",
            (uid,),
        )
        (awaiting_count,) = cur.fetchone()
    assert awaiting_count == 2
    conn.rollback()


def test_one_blocked_recipient_does_not_silence_the_others(conn, monkeypatch):
    """Mirrors `jobs.recurring`'s failure-isolation guard, for reconcile's own loop."""
    migrate(conn)
    conn.commit()  # baseline the test's own rows survive the job's commit
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (820300) RETURNING user_id")
        (blocked_uid,) = cur.fetchone()
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (820301) RETURNING user_id")
        (ok_uid,) = cur.fetchone()
    blocked_hh = household_of(conn, blocked_uid)
    ok_hh = household_of(conn, ok_uid)

    def flaky_send(chat_id, text, reply_markup=None):
        if chat_id == 820300:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        return {"result": {"message_id": 5000 + chat_id}}

    monkeypatch.setattr(reconcile, "send_message", flaky_send)

    events = []
    eventlog.bind_sink(events.append)
    try:
        with pytest.raises(DeliveryFailures) as caught:
            reconcile.run(conn)
    finally:
        eventlog.unbind_sink()

    assert caught.value.sent == 1
    assert [tg for tg, _ in caught.value.failures] == [820300]

    assert len(events) == 1
    assert events[0]["event"] == "job.reconcile"
    assert events[0]["status"] == "error"
    assert events[0]["delivered"] == 1
    assert events[0]["failed"] == 1

    assert pending_awaiting_reconcile(conn, blocked_uid) is None  # the failed send marked nothing
    assert pending_awaiting_reconcile(conn, ok_uid) is not None

    with conn.cursor() as cur:
        cur.execute("DELETE FROM pending_transactions WHERE user_id IN (%s, %s)",
                     (blocked_uid, ok_uid))
        cur.execute("DELETE FROM household_members WHERE household_id IN (%s, %s)",
                     (blocked_hh, ok_hh))
        cur.execute("DELETE FROM accounts WHERE household_id IN (%s, %s)", (blocked_hh, ok_hh))
        cur.execute("DELETE FROM households WHERE household_id IN (%s, %s)", (blocked_hh, ok_hh))
        cur.execute("DELETE FROM users WHERE telegram_user_id IN (820300, 820301)")
    conn.commit()
