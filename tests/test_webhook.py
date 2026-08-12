"""Webhook transport + update dispatch (§14, task 41).

Two things get checked here: `dispatch()` pulls the right action out of each
update shape, and the endpoint answers 200 to *every* body — the load-bearing
guard, since Telegram redelivers any update it did not get a 2xx for, so a
malformed body or an ignored update must not turn into a retry storm.
"""

from decimal import Decimal

import httpx
import pytest
from conftest import household_of
from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko import db, eventlog, handlers
from kanakko.app import WEBHOOK_SECRET_HEADER, app
from kanakko.categories import CATEGORY_PREFIX, EXPENSE_CATEGORIES
from kanakko.confirm import ACCOUNT_PREFIX, CANCEL, CONFIRM
from kanakko.db import get_or_create_user, save_pending, set_account_opening_balance
from kanakko.handlers import REMOVE_PREFIX, ButtonPress, TextMessage, dispatch
from kanakko.migrate import migrate
from kanakko.parse import Transaction

client = TestClient(app)

SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}


class _FakeConn:
    """Stand-in for a psycopg connection used as a `with connect() as conn` block."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _set_secret(monkeypatch, value=SECRET):
    if value is None:
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", value)


def test_text_message_is_dispatched_with_its_fields():
    # The update_id is the §17 correlation id — dispatch must carry it onto the
    # action, not drop it for the webhook to read separately (gap 1).
    action = dispatch(
        {
            "update_id": 4242,
            "message": {
                "message_id": 7,
                "chat": {"id": 42},
                "from": {"id": 99},
                "text": "spent 500 on food",
            },
        }
    )
    assert action == TextMessage(
        chat_id=42, message_id=7, text="spent 500 on food", from_id=99, update_id=4242
    )
    # §16: identity is the sender (from.id), send target is the chat (chat.id).
    assert action.from_id == 99 and action.chat_id == 42
    assert action.source == "webhook"


def test_button_press_is_dispatched_with_its_fields():
    action = dispatch(
        {
            "update_id": 4243,
            "callback_query": {
                "id": "cbq1",
                "data": "confirm",
                "from": {"id": 99},
                "message": {"message_id": 9, "chat": {"id": 42}},
            },
        }
    )
    assert action == ButtonPress(
        chat_id=42, message_id=9, callback_query_id="cbq1", data="confirm",
        from_id=99, update_id=4243,
    )
    # The tap's identity is the tapper (callback_query.from.id), not the chat.
    assert action.from_id == 99 and action.chat_id == 42
    assert action.source == "webhook"


def test_irrelevant_updates_are_ignored():
    # A non-text message (photo), an edited message, and an empty update all
    # route to nothing rather than being mistaken for a transaction.
    assert dispatch({"message": {"message_id": 1, "chat": {"id": 42}}}) is None
    assert dispatch({"edited_message": {"text": "typo fix"}}) is None
    assert dispatch({}) is None


def test_webhook_returns_200_for_a_text_update(monkeypatch):
    _set_secret(monkeypatch)
    # A handled text update returns 200; connect/handle_text are stubbed so this
    # exercises the routing, not a live DB. A handler exception is deliberately
    # left to 500 so Telegram redelivers a transiently-failed transaction.
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: None)
    response = client.post(
        "/webhook",
        json={"message": {"message_id": 1, "chat": {"id": 42}, "text": "spent 500 on food"}},
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
    monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

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


def test_handle_text_resolves_the_user_by_from_id_not_chat_id(conn, monkeypatch):
    """Identity is the sender (from_id), the chat is only the send target (§16).

    Impossible to write before the split: in a private chat from_id == chat_id, so
    a handler resolving by chat_id passed silently. Here from_id (777) differs from
    chat_id (12345), so the pending row must belong to 777 and *no* user row may
    exist for 12345 — resolving by chat_id would redden both asserts. The card
    still goes back to chat_id, which stays the delivery address.
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
    monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

    pending_id = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, from_id=777,
                          text="spent 500 on food")
    )
    assert sent["chat_id"] == 12345  # the card still goes to the chat

    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.telegram_user_id"
            " FROM pending_transactions p JOIN users u USING (user_id)"
            " WHERE pending_id = %s",
            (pending_id,),
        )
        (telegram_user_id,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM users WHERE telegram_user_id = %s", (12345,)
        )
        (chat_as_user,) = cur.fetchone()
    assert telegram_user_id == 777  # resolved by from_id, the sender
    assert chat_as_user == 0  # the chat id never became a user
    conn.rollback()


def test_handle_text_rejects_an_unparseable_message_with_a_rephrase(conn, monkeypatch):
    """No parseable amount → rephrase prompt, no pending row, no 500 (§3).

    `parse_message` raises `ValidationError` after its retry when the amount can't
    be read. `handle_text` must catch that, ask the user to rephrase, and store
    nothing — not let it propagate to a 500 that Telegram redelivers forever.
    """
    migrate(conn)

    def raise_validation(text, accounts=None):
        # A real amount-less parse: parse_amount rejects the empty amount, which
        # surfaces as the ValidationError parse_message re-raises after its retry.
        Transaction.model_validate(
            {
                "type": "expense",
                "amount": "",
                "category": EXPENSE_CATEGORIES[0],
                "date": "2026-08-06",
                "note": text,
            }
        )

    monkeypatch.setattr(handlers, "parse_message", raise_validation)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

    result = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="how's it going")
    )
    assert result is None  # nothing to confirm
    assert sent["text"] == handlers.REPHRASE_PROMPT
    assert sent["reply_markup"] is None  # a rephrase prompt, not a confirm card

    user_id = get_or_create_user(conn, 12345)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pending_transactions WHERE user_id = %s", (user_id,)
        )
        (pending_count,) = cur.fetchone()
    assert pending_count == 0  # §3: an unparseable message stores nothing
    conn.rollback()


API_KEY = "sk-or-v1-secret-key-value"


def _http_error(status_code, body=""):
    """An `httpx.HTTPStatusError` carrying `status_code`, as `parse_message` raises.

    The request carries a real `Authorization: Bearer <key>` header, exactly as
    `parse.call()` builds it. That is load-bearing for the no-leak guard below:
    with a bare request the assertion "the key is not in the log line" passes even
    if the log dumped `exc.request.headers`, because there would be no key there
    to leak. With the header present the guard can actually fail for the reason it
    exists (QA raised this on `991c883`).
    """
    request = httpx.Request(
        "POST", "https://openrouter.ai", headers={"Authorization": f"Bearer {API_KEY}"}
    )
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_handle_text_tells_the_user_when_a_4xx_parse_fails_permanently(conn, monkeypatch):
    """A 4xx from OpenRouter is permanent → warn the user, store nothing, no 500 (§6).

    402 (exhausted credits) is the outage that motivated this: Telegram redelivers
    a 500 forever, so a permanent upstream failure must be answered and swallowed,
    not looped. `handle_text` sends the parser-down message and returns None (webhook
    then answers 200).
    """
    migrate(conn)
    monkeypatch.setattr(
        handlers, "parse_message", lambda text, accounts=None: (_ for _ in ()).throw(_http_error(402))
    )
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

    result = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 on lunch")
    )
    assert result is None
    assert sent["text"] == handlers.PARSER_DOWN_PROMPT
    assert sent["reply_markup"] is None

    user_id = get_or_create_user(conn, 12345)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pending_transactions WHERE user_id = %s", (user_id,)
        )
        (pending_count,) = cur.fetchone()
    assert pending_count == 0  # §6: a permanent parse failure stores nothing
    conn.rollback()


def test_handle_text_lets_a_5xx_parse_failure_propagate(conn, monkeypatch):
    """A 5xx is transient → propagate so the webhook 500s and Telegram redelivers.

    The dangerous regression is swallowing *every* upstream error: that would pass a
    test covering only the 4xx branch while hiding a recoverable outage. So assert
    the other direction too — a 503 raises out of `handle_text` and sends nothing.
    """
    migrate(conn)
    monkeypatch.setattr(
        handlers, "parse_message", lambda text, accounts=None: (_ for _ in ()).throw(_http_error(503))
    )
    sent = []
    monkeypatch.setattr(handlers, "send_message", lambda *a, **k: sent.append(a))

    with pytest.raises(httpx.HTTPStatusError):
        app_module.handle_text(
            conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 on lunch")
        )
    assert sent == []  # 5xx redelivers; no user-facing message on the transient path
    conn.rollback()


def test_handle_text_logs_upstream_failure_at_warning_without_the_api_key(
    conn, monkeypatch, caplog
):
    """An upstream parse failure is logged at WARNING with status + body (§6, task 2).

    The 402 outage was invisible in the container log; the next occurrence must be
    diagnosable without reconstructing the request. Assert the status code and the
    provider body reach the log, and that the API key — which rides in the request
    headers, never the body — does not.
    """
    migrate(conn)
    body = "Insufficient credits. Add more at openrouter.ai/credits"
    monkeypatch.setattr(
        handlers, "parse_message", lambda text, accounts=None: (_ for _ in ()).throw(_http_error(402, body))
    )
    monkeypatch.setattr(
        handlers, "send_message", lambda *a, **k: {"result": {"message_id": 1}}
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)

    with caplog.at_level("WARNING", logger="kanakko.app"):
        app_module.handle_text(
            conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 on lunch")
        )

    (record,) = [r for r in caplog.records if r.levelname == "WARNING"]
    line = record.getMessage()
    assert "402" in line
    assert body in line
    # The exception really does carry the key (in its request headers), so this
    # assertion can fail — it is not passing merely because there is nothing to
    # leak. Logging `exc.request.headers` would redden it.
    assert API_KEY in str(dict(_http_error(402).request.headers))
    assert API_KEY not in line
    conn.rollback()


def test_handle_text_logs_the_parse_success_but_not_the_failure(conn, monkeypatch):
    """§17: a successful parse emits `parse.completed` with the model and a
    duration; a failed parse does not — Phase 6's WARNING owns the failure side, so
    a second `parse.completed` on the 402 path would double-log it and also lie that
    the parse succeeded. Assert both directions.
    """
    migrate(conn)
    monkeypatch.setattr(handlers, "send_message", lambda *a, **k: {"result": {"message_id": 909}})
    monkeypatch.setenv("OPENROUTER_MODEL", "acme/fast-1")
    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": "500.00",
            "category": EXPENSE_CATEGORIES[0],
            "date": "2026-08-06",
            "note": "lunch",
        }
    )

    events = []
    eventlog.bind_sink(events.append)
    try:
        monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
        app_module.handle_text(
            conn, TextMessage(chat_id=12345, message_id=1, text="spent 500", update_id=9)
        )
        monkeypatch.setattr(
            handlers, "parse_message", lambda text, accounts=None: (_ for _ in ()).throw(_http_error(402))
        )
        app_module.handle_text(
            conn, TextMessage(chat_id=12345, message_id=2, text="spent 500", update_id=10)
        )
    finally:
        eventlog.unbind_sink()

    completed = [e for e in events if e["event"] == "parse.completed"]
    assert len(completed) == 1  # only the success side; the 402 does not log here
    assert completed[0]["status"] == "ok"
    assert completed[0]["model"] == "acme/fast-1"  # the resolved model, greppable
    assert completed[0]["update_id"] == 9 and completed[0]["source"] == "webhook"
    assert isinstance(completed[0]["duration_ms"], int)
    conn.rollback()


def test_handle_text_shows_category_buttons_when_category_is_null(conn, monkeypatch):
    """A null category shows the category picker, not a confirm card (§3).

    `category: null` means the model couldn't tell, so instead of a card reading
    `Category: None` the user gets the closed set as `cat:<name>` buttons and a
    pending row is still stored (keyed by the sent card's id) for the eventual
    category press to update. The picker keyboard is *not* the Confirm/Cancel one:
    its buttons carry `cat:` callback_data, which is what this asserts — routing a
    null-category card through `confirm_card` would send Confirm/Cancel instead and
    would also trip its `assert txn.category is not None`.
    """
    migrate(conn)
    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": "500.00",
            "category": None,
            "date": "2026-08-06",
            "note": "spent 500 somewhere",
        }
    )
    monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

    pending_id = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 somewhere")
    )
    assert isinstance(pending_id, int)  # the row is still stored for the picker to update

    buttons = sent["reply_markup"].inline_keyboard
    cbs = [b.callback_data for row in buttons for b in row]
    assert all(c.startswith("cat:") for c in cbs)  # the category picker, not Confirm/Cancel
    assert CONFIRM not in cbs and CANCEL not in cbs

    with conn.cursor() as cur:
        cur.execute(
            "SELECT telegram_message_id, parsed FROM pending_transactions"
            " WHERE pending_id = %s",
            (pending_id,),
        )
        card_message_id, parsed = cur.fetchone()
    assert card_message_id == 909  # keyed on the sent picker's id
    assert parsed["category"] is None  # stored null, for the category press to fill in
    conn.rollback()


def test_handle_text_shows_a_confirm_card_not_a_category_picker_for_a_transfer(conn, monkeypatch):
    """A transfer's null category means "not applicable", not "couldn't tell" (§18).

    Every other null-category parse routes to the picker (the test above); a
    transfer's category is *always* null, so without the `type != "transfer"`
    guard `handle_text` would show category buttons for a credit-card bill
    payment — and `category_keyboard("transfer")` has no entry to build them
    from. It must go straight to the transfer's own Confirm/Cancel card instead.
    """
    migrate(conn)
    txn = Transaction.model_validate(
        {
            "type": "transfer",
            "amount": "2000.00",
            "category": None,
            "date": "2026-08-06",
            "note": "paid the card bill",
            "from_account": "Bank",
            "to_account": "Card",
        }
    )
    monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
    sent = {}

    def fake_send(chat_id, text, reply_markup=None):
        sent.update(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return {"ok": True, "result": {"message_id": 909}}

    monkeypatch.setattr(handlers, "send_message", fake_send)

    pending_id = app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="paid the card bill 2000")
    )
    assert isinstance(pending_id, int)
    assert "Bank → Card" in sent["text"]

    cbs = [b.callback_data for row in sent["reply_markup"].inline_keyboard for b in row]
    assert cbs == [CONFIRM, CANCEL]  # the transfer's own card, not the category picker
    conn.rollback()


def test_handle_text_threads_the_household_accounts_into_parse_message(conn, monkeypatch):
    """§18: the parse schema's account enum is built per request from the sender's
    own household, not asserted in name only — this checks `handle_text` actually
    looks the accounts up and passes them through, not just that `list_accounts`
    itself works (covered in `tests/test_accounts.py`).
    """
    migrate(conn)
    user_id = get_or_create_user(conn, 12345)
    household_of(conn, user_id)  # mints the default "Bank" account (§18)

    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": "500.00",
            "category": EXPENSE_CATEGORIES[0],
            "date": "2026-08-06",
            "note": "spent 500 on food",
        }
    )
    seen = {}

    def fake_parse(text, accounts=None):
        seen["accounts"] = accounts
        return txn

    monkeypatch.setattr(handlers, "parse_message", fake_parse)
    monkeypatch.setattr(
        handlers, "send_message",
        lambda *a, **k: {"ok": True, "result": {"message_id": 909}},
    )

    app_module.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="spent 500 on food")
    )
    assert seen["accounts"] == ["Bank"]  # the household's real account, not a literal
    conn.rollback()


def test_handle_text_shows_the_account_only_once_a_second_account_exists(conn, monkeypatch):
    """The confirm card's account line/buttons are the daily path's one real gate (§18).

    A single-account household (every onboarded user, by default) must send the
    exact pre-accounts card — the task's own acceptance line: "spent 500 on tea"
    with one account still confirms in one tap. Once a second account exists, the
    card grows an `Account:` line and `acct:<name>` buttons.
    """
    migrate(conn)

    def _card_for(user_id, chat_id):
        txn = Transaction.model_validate(
            {
                "type": "expense",
                "amount": "500.00",
                "category": EXPENSE_CATEGORIES[0],
                "date": "2026-08-06",
                "note": "spent 500 on tea",
            }
        )
        monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: txn)
        sent = {}
        monkeypatch.setattr(
            handlers, "send_message",
            lambda chat_id, text, reply_markup=None: sent.update(
                text=text, reply_markup=reply_markup
            ) or {"ok": True, "result": {"message_id": 909}},
        )
        app_module.handle_text(
            conn, TextMessage(chat_id=chat_id, message_id=1, text="spent 500 on tea")
        )
        return sent["text"], sent["reply_markup"]

    one_uid = get_or_create_user(conn, 20001)
    household_of(conn, one_uid)  # mints only the default "Bank" account
    text, keyboard = _card_for(one_uid, 20001)
    assert "Account:" not in text
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert not any(d.startswith(ACCOUNT_PREFIX) for d in data)

    two_uid = get_or_create_user(conn, 20002)
    household_of(conn, two_uid)
    set_account_opening_balance(conn, two_uid, "credit", Decimal("500.00"))  # mints "Card"
    text, keyboard = _card_for(two_uid, 20002)
    assert "Account: Bank" in text
    data = [b.callback_data for r in keyboard.inline_keyboard for b in r]
    assert {"acct:Bank", "acct:Card"} <= set(data)
    conn.rollback()


def _seed_pending(conn, chat_id, card_message_id, amount="100.00"):
    user_id = get_or_create_user(conn, chat_id)
    household_of(conn, user_id)  # confirm_pending homes the row in the user's household (§16)
    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": amount,
            "category": EXPENSE_CATEGORIES[0],
            "date": "2026-08-06",
            "note": "lunch",
        }
    )
    return user_id, save_pending(conn, user_id, card_message_id, txn)


def test_handle_confirm_writes_the_ledger_row_and_acknowledges(conn, monkeypatch):
    """A Confirm tap moves its pending row into the ledger and answers the tap (§4).

    Drives the real `confirm_pending` against Postgres; only the Telegram ack is
    stubbed. Asserts the amount lands as an exact `Decimal` (§9) through the
    `active_transactions` view (§6), and that the callback query is answered.
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    acked = {}
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acked.update(cbq=cbq, text=text)
    )
    monkeypatch.setattr(handlers, "edit_message_text", lambda *a, **k: None)

    txn_id = app_module.handle_confirm(
        conn, ButtonPress(chat_id=12345, message_id=909, callback_query_id="cbq1", data=CONFIRM)
    )
    assert isinstance(txn_id, int)
    assert acked == {"cbq": "cbq1", "text": "Saved ✅"}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT amount FROM active_transactions WHERE user_id = %s", (user_id,)
        )
        rows = cur.fetchall()
    assert rows == [(Decimal("100.00"),)]  # §9: exact Decimal, one row
    conn.rollback()


def test_handle_confirm_is_idempotent_on_a_redelivered_tap(conn, monkeypatch):
    """Telegram redelivers taps; a second Confirm must not double-write (§4)."""
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    acks = []
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acks.append(text)
    )
    monkeypatch.setattr(handlers, "edit_message_text", lambda *a, **k: None)
    press = ButtonPress(chat_id=12345, message_id=909, callback_query_id="cbq1", data=CONFIRM)

    first = app_module.handle_confirm(conn, press)
    second = app_module.handle_confirm(conn, press)
    assert isinstance(first, int)
    assert second is None  # nothing left to confirm
    assert acks == ["Saved ✅", "Already saved"]  # spinner cleared both times

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,)
        )
        (count,) = cur.fetchone()
    assert count == 1  # not two
    conn.rollback()


def test_handle_confirm_logs_ok_then_noop_on_a_redelivery(conn, monkeypatch):
    """The §17 event line follows the outcome: a real confirm is `ok` and carries
    the amount; the redelivered tap (pending row already gone) is `noop`, not a
    second `ok` that would inflate the count of confirms that actually happened.
    """
    migrate(conn)
    _seed_pending(conn, chat_id=12345, card_message_id=909)
    monkeypatch.setattr(handlers, "answer_callback_query", lambda *a, **k: None)
    monkeypatch.setattr(handlers, "edit_message_text", lambda *a, **k: None)
    press = ButtonPress(
        chat_id=12345, message_id=909, callback_query_id="c", data=CONFIRM, update_id=7
    )

    events = []
    eventlog.bind_sink(events.append)
    try:
        app_module.handle_confirm(conn, press)
        app_module.handle_confirm(conn, press)  # redelivery
    finally:
        eventlog.unbind_sink()

    assert [(e["event"], e["status"]) for e in events] == [
        ("transaction.confirmed", "ok"),
        ("transaction.confirmed", "noop"),
    ]
    assert events[0]["amount"] == Decimal("100.00")  # ledger path carries the amount
    assert events[0]["update_id"] == 7 and events[0]["source"] == "webhook"
    assert "amount" not in events[1]  # a noop touched no row
    conn.rollback()


def test_webhook_logs_exactly_one_error_line_when_a_handler_raises(monkeypatch):
    """The error side of §17 is logged once, in the webhook, not per handler (gap 5).

    A raising handler must produce exactly one `update.handled` with
    `status="error"` and then re-raise so the webhook still 500s and Telegram
    redelivers (§14). Folding the error log into every handler would multiply the
    line; keeping it in the one `try/except` around the dispatch is what makes
    "exactly one" true.
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)

    def boom(conn, msg):
        raise RuntimeError("handler down")

    monkeypatch.setattr(app_module, "handle_text", boom)

    events = []
    eventlog.bind_sink(events.append)
    try:
        with pytest.raises(RuntimeError):
            client.post(
                "/webhook",
                json={"message": {"message_id": 1, "chat": {"id": 42},
                                  "text": "spent 500 on food"}},
                headers=AUTH,
            )
    finally:
        eventlog.unbind_sink()

    errors = [e for e in events if e["status"] == "error"]
    assert len(errors) == 1  # not seven — logged in the webhook, not the handler
    assert errors[0]["event"] == "update.handled"
    assert errors[0]["source"] == "webhook"


def test_webhook_routes_confirm_and_cancel_to_their_handlers(monkeypatch):
    """CONFIRM routes to handle_confirm, CANCEL to handle_cancel — never crossed.

    Each button tap opens exactly one connection; an ignored update opens none.
    Routing CANCEL to handle_confirm would corrupt the ledger, so the split is
    asserted both ways. `connect` and the handlers are stubbed; no Postgres.
    """
    _set_secret(monkeypatch)
    opened = []
    monkeypatch.setattr(app_module, "connect", lambda: opened.append(True) or _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    confirmed, cancelled, texted, categorised = [], [], [], []
    monkeypatch.setattr(app_module, "handle_confirm", lambda conn, press: confirmed.append(press))
    monkeypatch.setattr(app_module, "handle_cancel", lambda conn, press: cancelled.append(press))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(app_module, "handle_category", lambda conn, press: categorised.append(press))

    def press(data):
        return {
            "callback_query": {
                "id": "cbq1",
                "data": data,
                "message": {"message_id": 909, "chat": {"id": 42}},
            }
        }

    client.post("/webhook", json=press(CONFIRM), headers=AUTH)
    assert len(confirmed) == 1 and len(cancelled) == 0 and len(opened) == 1

    client.post("/webhook", json=press(CANCEL), headers=AUTH)
    assert len(confirmed) == 1  # CANCEL did not route to the Confirm handler
    assert len(cancelled) == 1  # it routed to the Cancel handler
    assert len(opened) == 2  # and opened its own connection

    client.post("/webhook", json=press(f"{CATEGORY_PREFIX}Food"), headers=AUTH)
    assert len(categorised) == 1  # a cat: tap routes to the category handler
    assert len(confirmed) == 1 and len(cancelled) == 1  # not to Confirm/Cancel
    assert len(opened) == 3  # and opened its own connection

    accounted = []
    monkeypatch.setattr(
        app_module, "handle_account_choice", lambda conn, press: accounted.append(press)
    )
    client.post("/webhook", json=press(f"{ACCOUNT_PREFIX}Bank"), headers=AUTH)
    assert len(accounted) == 1  # an acct: tap routes to the account-choice handler
    assert len(categorised) == 1 and len(confirmed) == 1  # not to category/Confirm
    assert len(opened) == 4  # and opened its own connection

    chose = []
    monkeypatch.setattr(app_module, "handle_remove_choice", lambda conn, press: chose.append(press))
    client.post("/webhook", json=press(f"{REMOVE_PREFIX}delete:7"), headers=AUTH)
    assert len(chose) == 1  # an rm: tap routes to the removal-choice handler
    assert len(categorised) == 1 and len(confirmed) == 1  # not to category/Confirm
    assert len(opened) == 5

    client.post("/webhook", json={"channel_post": {"text": "x"}}, headers=AUTH)
    assert len(opened) == 5  # an ignored update opens nothing
    assert texted == []


def test_webhook_routes_undo_to_handle_undo_not_handle_text(monkeypatch):
    """`/undo` is a command, not a transaction — it must skip the parse path.

    Routing it to `handle_text` would feed "/undo" to the parser as if it were a
    transaction. The command is intercepted before that, and `/undo@bot` (the form
    Telegram sends in groups) resolves the same way. A plain text message still
    routes to `handle_text`.
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    undone, texted = [], []
    monkeypatch.setattr(app_module, "handle_undo", lambda conn, msg: undone.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))

    def text(body):
        return {"message": {"message_id": 1, "chat": {"id": 42}, "text": body}}

    client.post("/webhook", json=text("/undo"), headers=AUTH)
    assert len(undone) == 1 and texted == []

    client.post("/webhook", json=text("/undo@KanakkoBot"), headers=AUTH)
    assert len(undone) == 2  # the @bot suffix still routes to undo

    client.post("/webhook", json=text("spent 500 on food"), headers=AUTH)
    assert len(texted) == 1 and len(undone) == 2  # ordinary text still parses


def test_webhook_routes_invite_to_handle_invite_and_never_meters_it(monkeypatch):
    """`/invite` is an owner command, not a transaction, and must never be metered.

    Two failures this guards, both silent (`8a8bbc8` review finding 1): a routing
    miss would feed "/invite ravi" to `handle_text` — an LLM call and a garbage
    pending card; a metering miss would stamp its `claim_update` with `user_id`,
    so an owner at their daily cap could no longer invite and every invite would
    burn a cap unit. Asserts it routes to `handle_invite` (not `handle_text`) and
    that the claim is unmetered (`metered_user is None`). A plain text message
    still parses and still meters.
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    invited, texted, claims = [], [], []
    monkeypatch.setattr(app_module, "handle_invite", lambda conn, msg: invited.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(
        app_module, "claim_update", lambda conn, uid, metered: claims.append(metered) or True
    )

    def text(body, update_id):
        return {"update_id": update_id, "message": {"message_id": 1, "chat": {"id": 42}, "text": body}}

    client.post("/webhook", json=text("/invite ravi", 100), headers=AUTH)
    assert len(invited) == 1 and texted == []  # routed to invite, not the parser
    assert claims == [None]  # unmetered — never counts against the daily cap

    client.post("/webhook", json=text("spent 500 on food", 101), headers=AUTH)
    assert len(texted) == 1 and len(invited) == 1  # ordinary text still parses
    assert claims == [None, 1]  # and the parse path is metered with the user id


def test_webhook_routes_household_to_handle_household_and_never_meters_it(monkeypatch):
    """`/household` is a read, not a transaction, so it must never be metered.

    Same two silent failures as `/invite`: a routing miss would feed "/household"
    to `handle_text` (an LLM call and a junk pending card), and a metering miss
    would burn a daily-cap unit per lookup and could lock a capped user out of
    ever seeing their household. Asserts it routes to `handle_household` and the
    claim is unmetered (`metered_user is None`).
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    viewed, texted, claims = [], [], []
    monkeypatch.setattr(app_module, "handle_household", lambda conn, msg: viewed.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(
        app_module, "claim_update", lambda conn, uid, metered: claims.append(metered) or True
    )

    body = {"update_id": 200, "message": {"message_id": 1, "chat": {"id": 42}, "text": "/household"}}
    client.post("/webhook", json=body, headers=AUTH)

    assert len(viewed) == 1 and texted == []  # routed to the roster, not the parser
    assert claims == [None]  # unmetered — never counts against the daily cap


def test_webhook_routes_remove_to_handle_remove_and_never_meters_it(monkeypatch):
    """`/remove` is a household command, not a transaction, so it must never be metered.

    Same two silent failures as `/invite` and `/household`: a routing miss would
    feed "/remove ravi" to `handle_text` (an LLM call and a junk pending card), and
    a metering miss would burn a daily-cap unit per removal — letting a capped owner
    be unable to remove a member. Asserts it routes to `handle_remove` and the claim
    is unmetered (`metered_user is None`).
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    removed, texted, claims = [], [], []
    monkeypatch.setattr(app_module, "handle_remove", lambda conn, msg: removed.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(
        app_module, "claim_update", lambda conn, uid, metered: claims.append(metered) or True
    )

    body = {"update_id": 300, "message": {"message_id": 1, "chat": {"id": 42}, "text": "/remove ravi"}}
    client.post("/webhook", json=body, headers=AUTH)

    assert len(removed) == 1 and texted == []  # routed to removal, not the parser
    assert claims == [None]  # unmetered — never counts against the daily cap


def test_webhook_routes_transfer_to_handle_transfer_and_never_meters_it(monkeypatch):
    """`/transfer` is an owner command, not a transaction, so it must never be metered.

    Same two silent failures as `/invite`, `/household` and `/remove`: a routing
    miss would feed "/transfer ravi" to `handle_text` (an LLM call and a junk
    pending card), and a metering miss would burn a daily-cap unit — letting a
    capped owner be unable to hand off ownership. Asserts it routes to
    `handle_transfer` and the claim is unmetered (`metered_user is None`).
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    transferred, texted, claims = [], [], []
    monkeypatch.setattr(app_module, "handle_transfer", lambda conn, msg: transferred.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(
        app_module, "claim_update", lambda conn, uid, metered: claims.append(metered) or True
    )

    body = {"update_id": 300, "message": {"message_id": 1, "chat": {"id": 42}, "text": "/transfer ravi"}}
    client.post("/webhook", json=body, headers=AUTH)

    assert len(transferred) == 1 and texted == []  # routed to transfer, not the parser
    assert claims == [None]  # unmetered — never counts against the daily cap


def test_webhook_routes_account_to_handle_account_and_never_meters_it(monkeypatch):
    """`/account` sets up a credit/locked account, not a transaction, so it must
    never be metered.

    Same two silent failures as `/invite`, `/household`, `/remove` and `/transfer`:
    a routing miss would feed "/account credit 5000" to `handle_text` (an LLM call
    and a junk pending card), and a metering miss would burn a daily-cap unit per
    setup — locking a capped user out of ever adding a card. Asserts it routes to
    `handle_account` and the claim is unmetered (`metered_user is None`).
    """
    _set_secret(monkeypatch)
    monkeypatch.setattr(app_module, "connect", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "is_authorized", lambda conn, uid: True)
    monkeypatch.setattr(app_module, "get_or_create_user", lambda conn, uid: 1)
    monkeypatch.setattr(app_module, "within_daily_cap", lambda conn, uid: True)
    accounted, texted, claims = [], [], []
    monkeypatch.setattr(app_module, "handle_account", lambda conn, msg: accounted.append(msg))
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: texted.append(msg))
    monkeypatch.setattr(
        app_module, "claim_update", lambda conn, uid, metered: claims.append(metered) or True
    )

    body = {"update_id": 300, "message": {"message_id": 1, "chat": {"id": 42},
                                          "text": "/account credit 5000"}}
    client.post("/webhook", json=body, headers=AUTH)

    assert len(accounted) == 1 and texted == []  # routed to account setup, not the parser
    assert claims == [None]  # unmetered — never counts against the daily cap


def test_handle_undo_soft_deletes_the_last_row_and_confirms(conn, monkeypatch):
    """`/undo` removes the newest confirmed row and replies naming it (§5, §6).

    Drives the real `undo_last` against Postgres; only the Telegram send is
    stubbed. Asserts the row is gone from `active_transactions` (§6) and the reply
    names the removed amount via `format_amount` — an exact `Decimal`, not a float
    (§9).
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909, amount="250.00")
    row = db.confirm_pending(conn, user_id, 909, source="webhook", update_id=None)
    assert isinstance(row["txn_id"], int)
    sent = {}
    monkeypatch.setattr(
        handlers, "send_message", lambda chat_id, text, reply_markup=None: sent.update(chat_id=chat_id, text=text)
    )

    removed = app_module.handle_undo(
        conn, TextMessage(chat_id=12345, message_id=1, text="/undo")
    )
    assert removed is not None and removed["amount"] == Decimal("250.00")
    assert sent["chat_id"] == 12345
    assert "₹250.00" in sent["text"]  # §9: exact amount named in the reply

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,))
        assert cur.fetchone() == (0,)  # §6: soft-deleted, gone from the view
    conn.rollback()


def test_handle_undo_with_nothing_to_undo_replies_and_stores_nothing(conn, monkeypatch):
    """`/undo` with no live transaction replies "Nothing to undo." and returns None."""
    migrate(conn)
    sent = {}
    monkeypatch.setattr(
        handlers, "send_message", lambda chat_id, text, reply_markup=None: sent.update(text=text)
    )

    result = app_module.handle_undo(
        conn, TextMessage(chat_id=98765, message_id=1, text="/undo")
    )
    assert result is None
    assert sent["text"] == "Nothing to undo."
    conn.rollback()


def test_a_redelivered_undo_does_not_soft_delete_a_second_row(conn, monkeypatch):
    """The same `/undo`, redelivered, must remove exactly one transaction (§14, §6).

    Telegram redelivers any update it did not answer 2xx for, and `/undo` has no
    per-message anchor (unlike Confirm/Cancel), so without the `update_id` dedup a
    redelivery would soft-delete a *second* real row and silently drop it from
    every total. Two confirmed rows, then the identical `/undo` update POSTed
    twice: exactly one must survive in `active_transactions`, not zero. Drives the
    real webhook and `undo_last` against Postgres; only the Telegram send is
    stubbed. Reverting the webhook's `claim_update` guard reddens this
    (`assert 1 == 0` — both rows gone).
    """
    migrate(conn)
    uid, _ = _seed_pending(conn, chat_id=555, card_message_id=11, amount="250.00")
    db.confirm_pending(conn, uid, 11, source="webhook", update_id=None)
    _seed_pending(conn, chat_id=555, card_message_id=12, amount="100.00")
    db.confirm_pending(conn, uid, 12, source="webhook", update_id=None)

    monkeypatch.setattr(handlers, "send_message", lambda *a, **k: None)
    # connect() yields the shared test connection; __exit__ must not close it, so
    # the first delivery's claim is visible to the redelivery on the same session.
    class _Reuse:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(app_module, "connect", lambda: _Reuse())
    _set_secret(monkeypatch)

    update = {
        "update_id": 4242,
        "message": {"message_id": 7, "chat": {"id": 555}, "text": "/undo"},
    }
    client.post("/webhook", json=update, headers=AUTH)  # first delivery
    client.post("/webhook", json=update, headers=AUTH)  # redelivery, same update_id

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM active_transactions WHERE user_id = %s", (uid,))
        assert cur.fetchone() == (1,)  # one undone, one preserved — not both
    conn.rollback()


def test_handle_cancel_discards_the_pending_row_and_acknowledges(conn, monkeypatch):
    """A Cancel tap deletes its pending row, stores nothing, and answers the tap (§5).

    Drives the real `cancel_pending` against Postgres; only the Telegram ack is
    stubbed. Asserts the pending row is gone and no ledger row was written.
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    acked = {}
    removed = []
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acked.update(cbq=cbq, text=text)
    )
    monkeypatch.setattr(
        handlers, "delete_message", lambda chat_id, message_id: removed.append((chat_id, message_id)) or True
    )

    pending_id = app_module.handle_cancel(
        conn, ButtonPress(chat_id=12345, message_id=909, callback_query_id="cbq1", data=CANCEL)
    )
    assert isinstance(pending_id, int)
    assert acked == {"cbq": "cbq1", "text": "Discarded ❌"}
    # The cancelled card is taken out of the chat, not left as a dead card with
    # live buttons — and it is *that* card, by the id the tap carried.
    assert removed == [(12345, 909)]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pending_transactions WHERE user_id = %s", (user_id,)
        )
        (pending_count,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,)
        )
        (ledger_count,) = cur.fetchone()
    assert pending_count == 0  # the pending row is gone
    assert ledger_count == 0  # §5: Cancel writes nothing to the ledger
    conn.rollback()


def test_handle_cancel_is_idempotent_on_a_redelivered_tap(conn, monkeypatch):
    """Telegram redelivers taps; a second Cancel must still ack, not error (§5)."""
    migrate(conn)
    _seed_pending(conn, chat_id=12345, card_message_id=909)
    acks = []
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acks.append(text)
    )
    # A redelivered Cancel deletes a card that is already gone: the Bot API
    # answers 400, `delete_message` returns False, and the handler must carry on
    # to the ack rather than raise — a raise here would 500 and make Telegram
    # redeliver the same tap forever.
    monkeypatch.setattr(handlers, "delete_message", lambda chat_id, message_id: False)
    press = ButtonPress(chat_id=12345, message_id=909, callback_query_id="cbq1", data=CANCEL)

    first = app_module.handle_cancel(conn, press)
    second = app_module.handle_cancel(conn, press)
    assert isinstance(first, int)
    assert second is None  # nothing left to cancel
    assert acks == ["Discarded ❌", "Already gone"]  # spinner cleared both times
    conn.rollback()


def test_confirm_settles_the_card_and_a_stale_cancel_leaves_the_receipt(conn, monkeypatch):
    """A confirmed card settles with no keyboard, and a later Cancel can't wipe it.

    Two halves, both asserted (a card that only read the new text would pass with
    the stale-Cancel trap still there):
    (a) after Confirm the card is edited into a settled receipt carrying **no**
        `reply_markup` — nothing left to re-tap;
    (b) a Cancel arriving on that now-confirmed card finds no pending row, so it
        must delete **nothing** and leave the ledger row in place — the transcript
        and the ledger must not disagree (the stale-Cancel receipt bug).
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    edits, deletes = [], []
    monkeypatch.setattr(handlers, "answer_callback_query", lambda cbq, text=None: None)
    monkeypatch.setattr(
        handlers,
        "edit_message_text",
        lambda chat_id, message_id, text, reply_markup=None: edits.append(
            (message_id, text, reply_markup)
        ),
    )
    monkeypatch.setattr(
        handlers,
        "delete_message",
        lambda chat_id, message_id: deletes.append((chat_id, message_id)) or True,
    )

    app_module.handle_confirm(
        conn, ButtonPress(chat_id=12345, message_id=909, callback_query_id="c1", data=CONFIRM)
    )
    # (a) the card became a settled receipt with no buttons to re-tap
    assert len(edits) == 1
    edited_id, edited_text, edited_markup = edits[0]
    assert edited_id == 909
    assert edited_markup is None  # no Confirm/Cancel left on a saved card
    assert "Saved" in edited_text

    # (b) a stale Cancel on the confirmed card deletes nothing, ledger untouched
    result = app_module.handle_cancel(
        conn, ButtonPress(chat_id=12345, message_id=909, callback_query_id="c2", data=CANCEL)
    )
    assert result is None  # no pending row — Confirm already cleared it
    assert deletes == []  # the receipt is NOT removed from the chat

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM active_transactions WHERE user_id = %s", (user_id,)
        )
        (count,) = cur.fetchone()
    assert count == 1  # the confirmed transaction stays in the ledger
    conn.rollback()


def test_handle_category_updates_the_pending_row_and_re_renders_the_card(conn, monkeypatch):
    """A `cat:<name>` tap sets the category and edits the picker into a full card (§5).

    Seeds a null-category pending row (the picker case) keyed on the sent card.
    Tapping a category must (1) write that category to the pending row so a later
    Confirm stores it, and (2) edit the *same* message into a confirm card — one
    now carrying Confirm/Cancel, which the null-category picker lacked. Drives the
    real `set_pending_category` against Postgres; only Telegram I/O is stubbed.
    """
    migrate(conn)
    user_id = get_or_create_user(conn, 12345)
    txn = Transaction.model_validate(
        {
            "type": "expense",
            "amount": "500.00",
            "category": None,
            "date": "2026-08-06",
            "note": "spent 500 somewhere",
        }
    )
    save_pending(conn, user_id, 909, txn)
    edited = {}
    acked = {}
    monkeypatch.setattr(
        handlers,
        "edit_message_text",
        lambda chat_id, message_id, text, reply_markup=None: edited.update(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
        ),
    )
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acked.update(cbq=cbq, text=text)
    )

    chosen = EXPENSE_CATEGORIES[0]
    result = app_module.handle_category(
        conn,
        ButtonPress(
            chat_id=12345, message_id=909, callback_query_id="cbq1", data=f"{CATEGORY_PREFIX}{chosen}"
        ),
    )
    assert result is not None and result.category == chosen
    assert acked == {"cbq": "cbq1", "text": f"Category: {chosen}"}

    # The picker was edited into a full confirm card (Confirm/Cancel now present).
    assert edited["chat_id"] == 12345 and edited["message_id"] == 909
    cbs = [b.callback_data for row in edited["reply_markup"].inline_keyboard for b in row]
    assert CONFIRM in cbs and CANCEL in cbs

    with conn.cursor() as cur:
        cur.execute("SELECT parsed FROM pending_transactions WHERE telegram_message_id = 909")
        (parsed,) = cur.fetchone()
    assert parsed["category"] == chosen  # the row now carries the chosen category
    conn.rollback()


def test_handle_category_ignores_a_forged_unknown_category(conn, monkeypatch):
    """A `cat:` callback the keyboard never emits is acked and ignored, not 500ed.

    A raise on an unknown category would make Telegram redeliver the bad tap
    forever (the same trap task 72 fixed for unparseable text). The pending row
    must be left untouched and nothing edited.
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    edits = []
    monkeypatch.setattr(
        handlers, "edit_message_text", lambda *a, **k: edits.append(a)
    )
    monkeypatch.setattr(handlers, "answer_callback_query", lambda cbq, text=None: None)

    result = app_module.handle_category(
        conn,
        ButtonPress(chat_id=12345, message_id=909, callback_query_id="cbq1", data="cat:Bogus"),
    )
    assert result is None
    assert edits == []  # nothing re-rendered

    with conn.cursor() as cur:
        cur.execute("SELECT parsed FROM pending_transactions WHERE telegram_message_id = 909")
        (parsed,) = cur.fetchone()
    assert parsed["category"] == EXPENSE_CATEGORIES[0]  # unchanged
    conn.rollback()


def test_handle_account_choice_updates_the_pending_row_and_re_renders_the_card(conn, monkeypatch):
    """An `acct:<name>` tap sets the account and edits the card in place (§18, §5).

    The account picker's counterpart to `test_handle_category_updates_…`: the
    tap must (1) write the chosen account to the pending row so a later Confirm
    stores it there (`kanakko.db.pending.confirm_pending` reads `txn.account`),
    and (2) re-render the same message. Drives the real `set_pending_account`
    against Postgres; only Telegram I/O is stubbed.
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    set_account_opening_balance(conn, user_id, "credit", Decimal("500.00"))  # mints "Card"
    edited = {}
    acked = {}
    monkeypatch.setattr(
        handlers,
        "edit_message_text",
        lambda chat_id, message_id, text, reply_markup=None: edited.update(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
        ),
    )
    monkeypatch.setattr(
        handlers, "answer_callback_query", lambda cbq, text=None: acked.update(cbq=cbq, text=text)
    )

    result = app_module.handle_account_choice(
        conn,
        ButtonPress(
            chat_id=12345, message_id=909, callback_query_id="cbq1", data=f"{ACCOUNT_PREFIX}Card"
        ),
    )
    assert result is not None and result.account == "Card"
    assert acked == {"cbq": "cbq1", "text": "Account: Card"}
    assert edited["chat_id"] == 12345 and edited["message_id"] == 909
    assert "Account: Card" in edited["text"]

    with conn.cursor() as cur:
        cur.execute("SELECT parsed FROM pending_transactions WHERE telegram_message_id = 909")
        (parsed,) = cur.fetchone()
    assert parsed["account"] == "Card"  # the row now carries the chosen account
    conn.rollback()


def test_handle_account_choice_ignores_an_account_outside_the_household(conn, monkeypatch):
    """An `acct:` callback naming an account outside this user's household is ignored.

    Unlike category (a module-level closed set the keyboard itself can't
    misrepresent), the account set is per household — a stale or forged tap
    naming another household's account, or an account since deleted, must not
    500 or silently misfile the transaction. The pending row is left untouched
    and nothing is re-rendered.
    """
    migrate(conn)
    user_id, _ = _seed_pending(conn, chat_id=12345, card_message_id=909)
    edits = []
    monkeypatch.setattr(handlers, "edit_message_text", lambda *a, **k: edits.append(a))
    monkeypatch.setattr(handlers, "answer_callback_query", lambda cbq, text=None: None)

    result = app_module.handle_account_choice(
        conn,
        ButtonPress(
            chat_id=12345, message_id=909, callback_query_id="cbq1",
            data=f"{ACCOUNT_PREFIX}Bogus",
        ),
    )
    assert result is None
    assert edits == []  # nothing re-rendered

    with conn.cursor() as cur:
        cur.execute("SELECT parsed FROM pending_transactions WHERE telegram_message_id = 909")
        (parsed,) = cur.fetchone()
    assert parsed["account"] is None  # unchanged
    conn.rollback()


def test_cancel_is_scoped_to_the_user(conn):
    """A user's Cancel can't discard another user's identically-numbered card (§1)."""
    migrate(conn)
    a_user, _ = _seed_pending(conn, chat_id=111, card_message_id=555, amount="100.00")
    b_user, _ = _seed_pending(conn, chat_id=222, card_message_id=555, amount="999.99")

    cancelled = db.cancel_pending(conn, a_user, 555)
    assert cancelled is not None

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pending_transactions WHERE user_id = %s", (b_user,))
        (b_remaining,) = cur.fetchone()
    assert b_remaining == 1  # B's pending card is untouched
    conn.rollback()
