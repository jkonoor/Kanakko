"""The §16 authorization gate: an unrecognised user is refused before any LLM call.

§16 closes a live hole — the bot is already multi-user, so anyone who finds it
gets a private ledger parsed on the operator's OpenRouter credits. The gate turns
an unknown user away in `invite` mode *before* the parse call (§2 costs money) and
stores nothing — not even a user row. Two things must both hold: no OpenRouter
call, and no rows at all. A gate that refused but still minted a user row, or that
still reached the parser, would defeat the point, so the core test asserts both
directions. The counter-direction tests keep the gate from being "refuse
everyone": a known user and any user in `open` mode are still served.
"""

from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko import handlers
from kanakko.app import WEBHOOK_SECRET_HEADER, app
from kanakko.db import get_or_create_user
from kanakko.migrate import migrate

client = TestClient(app)

SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}
UNKNOWN = 424242
UPDATE_ID = 700


def _reuse_conn(conn):
    """`connect()` yields the shared test connection; __exit__ must not close or
    commit it, so writes stay in the transaction the test rolls back."""

    class _Reuse:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    return lambda: _Reuse()


def _text_update(from_id, chat_id=None):
    chat_id = chat_id if chat_id is not None else from_id
    return {
        "update_id": UPDATE_ID,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": from_id},
            "text": "spent 500 on food",
        },
    }


def test_unknown_user_in_invite_mode_is_refused_and_stores_nothing(conn, monkeypatch):
    """Invite mode: an unknown sender is refused, makes no LLM call, and writes no row.

    This is the whole reason §16's gate exists. Reverting the `is_authorized` check
    in the webhook reddens this: `handle_text` would run, `parse_message` would be
    called (a paid OpenRouter request), and `get_or_create_user`/`claim_update`
    would mint rows for a user who was never admitted.
    """
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    parse_calls = []
    monkeypatch.setattr(handlers, "parse_message", lambda text, accounts=None: parse_calls.append(text))
    sent = []
    monkeypatch.setattr(app_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    response = client.post("/webhook", json=_text_update(UNKNOWN), headers=AUTH)

    assert response.status_code == 200  # a refusal still answers 200, never a retry
    assert sent == [(UNKNOWN, handlers.ACCESS_REFUSED)]  # the polite refusal, once
    assert parse_calls == []  # the cost the gate exists to stop: no OpenRouter call

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = %s", (UNKNOWN,))
        (users,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM processed_updates WHERE update_id = %s", (UPDATE_ID,))
        (claimed,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM pending_transactions")
        (pending,) = cur.fetchone()
    assert users == 0  # §16: not even a user row
    assert claimed == 0  # the update was never claimed
    assert pending == 0  # nothing parsed, nothing stored
    conn.rollback()


def test_known_user_in_invite_mode_is_served(conn, monkeypatch):
    """A user who already has a row is admitted — the gate is not "refuse everyone".

    Without this the previous test would pass a gate that turned *everyone* away.
    A pre-existing user reaches `handle_text` and is not sent the refusal.
    """
    migrate(conn)
    get_or_create_user(conn, UNKNOWN)  # now a known user
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    handled = []
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: handled.append(msg))
    sent = []
    monkeypatch.setattr(app_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    response = client.post("/webhook", json=_text_update(UNKNOWN), headers=AUTH)

    assert response.status_code == 200
    assert len(handled) == 1  # reached the handler
    assert sent == []  # not refused
    conn.rollback()


def test_open_mode_admits_an_unknown_user(conn, monkeypatch):
    """`open` mode serves anyone — going public is one env change (§16)."""
    migrate(conn)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "open")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    handled = []
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: handled.append(msg))
    sent = []
    monkeypatch.setattr(app_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))

    response = client.post("/webhook", json=_text_update(UNKNOWN), headers=AUTH)

    assert response.status_code == 200
    assert len(handled) == 1  # an unknown user is served under open signup
    assert sent == []  # not refused
    conn.rollback()
