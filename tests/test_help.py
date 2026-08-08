"""A lost user gets help, and doesn't pay for it (§ chat polish).

"How do I use this?" used to be answered with a complaint — *"I couldn't find an
amount in that"* — after two OpenRouter calls and two slots of the daily cap. The
fix: `/help` and an exact-match greeting short-circuit *before* the parse, sending
the same orienting text and making zero LLM calls.

The whole safety argument is the exact match: a heuristic ("no digits", "ends in
?") would eventually swallow a real expense. So the check asserts both directions —
`hi` never reaches the parser, and `spent five hundred on lunch` still does.
Reverting the `_is_greeting` short-circuit in the webhook routing reddens the first;
a filter loose enough to swallow the expense reddens the second.
"""

from datetime import date

from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko import handlers
from kanakko.app import WEBHOOK_SECRET_HEADER, app
from kanakko.db import count_updates_on_day, get_or_create_user
from kanakko.migrate import migrate

client = TestClient(app)

SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}
USER = 777


def _reuse_conn(conn):
    class _Reuse:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    return lambda: _Reuse()


def _text_update(update_id, text, from_id=USER):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": from_id},
            "from": {"id": from_id},
            "text": text,
        },
    }


def test_greeting_and_help_short_circuit_the_parser_but_an_expense_still_reaches_it(
    conn, monkeypatch
):
    """`hi`, `/help`, and "how do i use this?" answer with help and never parse;
    "spent five hundred on lunch" (no digits) still reaches the parse path.

    Asserting the call count, not just the reply: the parse path (`handle_text`) is
    recorded and must stay empty for every help form and fire exactly once for the
    real expense. A greeting must also cost nothing — it is never metered against the
    daily cap.
    """
    migrate(conn)
    uid = get_or_create_user(conn, USER)  # a known user, so the gate admits them
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))

    parsed = []
    monkeypatch.setattr(app_module, "handle_text", lambda conn, msg: parsed.append(msg.text))
    sent = []
    monkeypatch.setattr(handlers, "send_message", lambda chat_id, text: sent.append(text))

    for update_id, text in [(1, "hi"), (2, "/help"), (3, "How do I use this?")]:
        sent.clear()
        response = client.post("/webhook", json=_text_update(update_id, text), headers=AUTH)
        assert response.status_code == 200
        assert sent == [handlers.REPHRASE_PROMPT]  # the help text, not a complaint
        assert parsed == []  # never reached the parser — zero OpenRouter calls

    # A greeting is free: it never counts against the §16 daily cap.
    assert count_updates_on_day(conn, uid, date.today()) == 0

    # The real expense — no digits, so a lazy "has a number?" filter would drop it —
    # still reaches the parse path.
    response = client.post(
        "/webhook", json=_text_update(4, "spent five hundred on lunch"), headers=AUTH
    )
    assert response.status_code == 200
    assert parsed == ["spent five hundred on lunch"]

    conn.rollback()
