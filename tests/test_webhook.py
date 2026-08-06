"""Webhook transport + update dispatch (§14, task 41).

Two things get checked here: `dispatch()` pulls the right action out of each
update shape, and the endpoint answers 200 to *every* body — the load-bearing
guard, since Telegram redelivers any update it did not get a 2xx for, so a
malformed body or an ignored update must not turn into a retry storm.
"""

from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko.app import WEBHOOK_SECRET_HEADER, ButtonPress, TextMessage, app, dispatch
from kanakko.categories import EXPENSE_CATEGORIES
from kanakko.migrate import migrate
from kanakko.parse import Transaction

client = TestClient(app)

SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}


def _set_secret(monkeypatch, value=SECRET):
    if value is None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", value)


def test_text_message_is_dispatched_with_its_fields():
    action = dispatch(
        {"message": {"message_id": 7, "chat": {"id": 42}, "text": "spent 500 on food"}}
    )
    assert action == TextMessage(chat_id=42, message_id=7, text="spent 500 on food")


def test_button_press_is_dispatched_with_its_fields():
    action = dispatch(
        {
            "callback_query": {
                "id": "cbq1",
                "data": "confirm",
                "message": {"message_id": 9, "chat": {"id": 42}},
            }
        }
    )
    assert action == ButtonPress(
        chat_id=42, message_id=9, callback_query_id="cbq1", data="confirm"
    )


def test_irrelevant_updates_are_ignored():
    # A non-text message (photo), an edited message, and an empty update all
    # route to nothing rather than being mistaken for a transaction.
    assert dispatch({"message": {"message_id": 1, "chat": {"id": 42}}}) is None
    assert dispatch({"edited_message": {"text": "typo fix"}}) is None
    assert dispatch({}) is None


def test_webhook_returns_200_for_a_text_update(monkeypatch):
    _set_secret(monkeypatch)
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "hi"}},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_returns_200_for_an_ignored_update(monkeypatch):
    _set_secret(monkeypatch)
    response = client.post(
        "/webhook", json={"channel_post": {"text": "spam"}}, headers=AUTH
    )
    assert response.status_code == 200


def test_webhook_returns_200_for_a_malformed_body(monkeypatch):
    # Not JSON. A 500 here would make Telegram redeliver this forever.
    _set_secret(monkeypatch)
    response = client.post(
        "/webhook",
        content=b"not json",
        headers={"content-type": "application/json", **AUTH},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_rejects_a_missing_secret_header(monkeypatch):
    # §15 — a forged POST without Telegram's secret_token gets 403, and its
    # body never reaches dispatch.
    _set_secret(monkeypatch)
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "hi"}},
    )
    assert response.status_code == 403


def test_webhook_rejects_a_wrong_secret(monkeypatch):
    _set_secret(monkeypatch)
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "hi"}},
        headers={WEBHOOK_SECRET_HEADER: "not-the-secret"},
    )
    assert response.status_code == 403


def test_webhook_fails_closed_when_the_secret_is_unset(monkeypatch):
    # §15 — an unset secret must never mean "accept everything". Even a request
    # presenting an empty token (which would match an empty secret under ==)
    # is rejected.
    _set_secret(monkeypatch, value=None)
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "hi"}},
        headers={WEBHOOK_SECRET_HEADER: ""},
    )
    assert response.status_code == 403


def test_handle_text_keys_the_pending_row_on_the_sent_card(conn, monkeypatch):
    """The parsed message is stored keyed by the confirm card's message id (§1, §4).

    Confirm/Cancel taps carry back the *card's* message id, so `save_pending` must
    key on that — not on the user's inbound message id. The fake send returns a
    different id (909) from the inbound message (1), so keying on the wrong one
    reddens the `== 909` assert. Parse and send are stubbed — no network.
    """
    migrate(conn)
    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": "500.00",
            "category": EXPENSE_CATEGORIES[0],
            "date": "2026-08-06",
            "note": "spent 500 on food",
        }
    )
    monkeypatch.setattr(app_module, "parse_message", lambda text: txn)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(app_module, "send_message", fake_send)

    pending_id = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 on food")
    )
    assert isinstance(pending_id, int)
    assert sent["chat_id"] == 12345  # the card goes back to the sender

    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id, u.telegram_user_id, parsed"
            " FROM pending_transactions p JOIN users u USING (user_id)"
            " WHERE pending_id = %s",
            (pending_id,),
        )
        card_message_id, telegram_user_id, parsed = cur.fetchone()
    assert card_message_id == 909  # the card's id, not the inbound message's (1)
    assert telegram_user_id == 12345  # chat id resolved to a users row
    assert parsed["amount"] == "500.00"  # §9: stored as a string, not a float
    conn.rollback()
