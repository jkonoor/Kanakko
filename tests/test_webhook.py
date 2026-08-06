"""Webhook transport + update dispatch (§14, task 41).

Two things get checked here: `dispatch()` pulls the right action out of each
update shape, and the endpoint answers 200 to *every* body — the load-bearing
guard, since Telegram redelivers any update it did not get a 2xx for, so a
malformed body or an ignored update must not turn into a retry storm.
"""

from fastapi.testclient import TestClient

from kanakko.app import ButtonPress, TextMessage, app, dispatch

client = TestClient(app)


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


def test_webhook_returns_200_for_a_text_update():
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "hi"}},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_returns_200_for_an_ignored_update():
    response = client.post("/webhook", json={"channel_post": {"text": "spam"}})
    assert response.status_code == 200


def test_webhook_returns_200_for_a_malformed_body():
    # Not JSON. A 500 here would make Telegram redeliver this forever.
    response = client.post(
        "/webhook", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
