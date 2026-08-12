"""The reconcile reply — the other half of the weekly nudge (§18).

`handle_reconcile_reply` is `confirm_flow.handle_amount_reply`'s sibling: no
LLM call, no pending row of its own, just an amount reply against an
outstanding ask. Drives the real `db` layer against Postgres; only Telegram I/O
is stubbed.
"""

from decimal import Decimal

from conftest import household_of

from kanakko import reconcile_flow
from kanakko.db import create_reconcile_ask, pending_awaiting_reconcile
from kanakko.handlers import TextMessage
from kanakko.migrate import migrate
from kanakko.reconcile_flow import handle_reconcile_reply


def _account(conn, hh, uid, *, kind, name, opening="0", is_default=False):
    with conn.cursor() as cur:
        if is_default:
            cur.execute(
                "UPDATE accounts SET name = %s, opening_balance = %s"
                " WHERE household_id = %s AND is_default AND deleted_at IS NULL"
                " RETURNING account_id",
                (name, opening, hh),
            )
            return cur.fetchone()[0]
        cur.execute(
            "INSERT INTO accounts (household_id, owner, kind, name, opening_balance, is_default)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING account_id",
            (hh, uid, kind, name, opening, is_default),
        )
        return cur.fetchone()[0]


def test_a_mismatched_reply_writes_a_visible_adjustment_and_clears_the_ask(conn, monkeypatch):
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (830100) RETURNING user_id")
        (uid,) = cur.fetchone()
    hh = household_of(conn, uid)
    bank = _account(conn, hh, uid, kind="spending", name="Bank", opening="1000", is_default=True)
    create_reconcile_ask(conn, uid, 909, bank)

    sent = []
    monkeypatch.setattr(reconcile_flow, "send_message", lambda chat_id, text: sent.append(text))

    row = handle_reconcile_reply(
        conn, TextMessage(chat_id=830100, message_id=1, text="1200"), 909, bank
    )

    assert row is not None and row["amount"] == Decimal("200.00")
    assert len(sent) == 1 and "₹200.00" in sent[0]
    assert pending_awaiting_reconcile(conn, uid) is None  # the ask is settled
    conn.rollback()


def test_a_matching_reply_writes_nothing_and_clears_the_ask(conn, monkeypatch):
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (830200) RETURNING user_id")
        (uid,) = cur.fetchone()
    hh = household_of(conn, uid)
    bank = _account(conn, hh, uid, kind="spending", name="Bank", opening="1000", is_default=True)
    create_reconcile_ask(conn, uid, 910, bank)

    sent = []
    monkeypatch.setattr(reconcile_flow, "send_message", lambda chat_id, text: sent.append(text))

    row = handle_reconcile_reply(
        conn, TextMessage(chat_id=830200, message_id=1, text="1000"), 910, bank
    )

    assert row is None
    assert sent == [reconcile_flow.RECONCILE_MATCHED]
    assert pending_awaiting_reconcile(conn, uid) is None  # answered, even with no drift
    conn.rollback()


def test_an_unparseable_reply_leaves_the_ask_outstanding(conn, monkeypatch):
    """The user can just retry — the same contract "Change amount" gives (§18)."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (830300) RETURNING user_id")
        (uid,) = cur.fetchone()
    hh = household_of(conn, uid)
    bank = _account(conn, hh, uid, kind="spending", name="Bank", opening="1000", is_default=True)
    create_reconcile_ask(conn, uid, 911, bank)

    sent = []
    monkeypatch.setattr(reconcile_flow, "send_message", lambda chat_id, text: sent.append(text))

    row = handle_reconcile_reply(
        conn, TextMessage(chat_id=830300, message_id=1, text="not a number"), 911, bank
    )

    assert row is None
    assert sent == [reconcile_flow.RECONCILE_RETRY_PROMPT]
    assert pending_awaiting_reconcile(conn, uid) == {
        "telegram_message_id": 911, "account_id": bank,
    }
    conn.rollback()


def test_a_credit_account_sign_convention_carries_through_the_reply(conn, monkeypatch):
    """The reply flow doesn't re-derive the sign — it trusts `create_adjustment`."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (telegram_user_id) VALUES (830400) RETURNING user_id")
        (uid,) = cur.fetchone()
    hh = household_of(conn, uid)
    card = _account(conn, hh, uid, kind="credit", name="Card", opening="-500")
    create_reconcile_ask(conn, uid, 912, card)

    monkeypatch.setattr(reconcile_flow, "send_message", lambda chat_id, text: None)

    row = handle_reconcile_reply(
        conn, TextMessage(chat_id=830400, message_id=1, text="700"), 912, card
    )

    assert row is not None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_account_id, to_account_id FROM transactions WHERE txn_id = %s",
            (row["txn_id"],),
        )
        from_id, to_id = cur.fetchone()
    assert from_id == card and to_id != card  # deepens the debt, like an ordinary swipe
    conn.rollback()
