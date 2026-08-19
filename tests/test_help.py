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
    # `**kw` because `handle_help` passes `parse_mode="HTML"` and the other
    # senders on this path pass nothing — the stub must accept both shapes.
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text, **kw: sent.append(text))

    for update_id, text in [(1, "hi"), (2, "/help"), (3, "How do I use this?")]:
        sent.clear()
        response = client.post("/webhook", json=_text_update(update_id, text), headers=AUTH)
        assert response.status_code == 200
        assert sent == [handlers.HELP_TEXT]  # the manual, not the parse-failure nudge
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


def test_help_lists_every_command_and_the_failed_parse_stays_a_correction():
    """`/help` is the only place the household commands are named, and the
    parse-failure reply must still say what went wrong (§ chat polish).

    `/household` named `/invite` only in its solo reply, and `/remove` and
    `/transfer` described themselves only inside their own error paths — which a
    user cannot reach without already knowing the command exists. So the help text
    is the index, and this asserts every command is in it: a command added later
    without a line here is undiscoverable, and nothing else would go red.

    The second half guards the regression that prompted the split. When one string
    served both jobs, a real expense that failed to parse was answered with a
    description of the bot and never told the user the *amount* was the problem.
    """
    for command in ("/undo", "/household", "/invite", "/remove", "/transfer", "/recurring", "refund"):
        assert command in handlers.HELP_TEXT, f"{command} is undiscoverable"

    # Names the menu button as it reads on screen (docs/DEPLOYMENT.md), so the
    # user maps a word to something visible rather than decoding "menu button".
    assert "Dashboard" in handlers.HELP_TEXT

    # The correction is a nudge about the amount, not the manual.
    assert "amount" in handlers.REPHRASE_PROMPT
    assert handlers.REPHRASE_PROMPT != handlers.HELP_TEXT
    assert "/undo" not in handlers.REPHRASE_PROMPT


def test_setting_a_spending_account_balance_is_pointed_at_from_welcome_and_help():
    """A new household's default account sits at ₹0 until someone tells the bot
    what's really in it — `/account bank 52000` does that (Phase 11), but nothing
    ever said so. Both the first message a user sees and the permanent manual
    must name it, or the fix stays undiscoverable exactly like the commands the
    test above already guards.
    """
    assert "/account bank" in handlers.WELCOME
    assert "/account bank" in handlers.HELP_TEXT


def test_no_sent_message_contains_a_literal_backtick():
    """`tg.send_message` sends plain text unless a call opts in, so a backtick
    reaches Telegram as a backtick — every usage hint read `/invite ravi` with the
    marks visible, across 21 strings, for months.

    A markup mode is not the general alternative: most of these messages
    interpolate user-typed labels and account names, and one unbalanced `*` or
    stray `<` in a label would make Telegram reject the whole send, leaving the
    bot silent on that path. `HELP_TEXT` is the single exception, and only
    because it is entirely static — see the HTML guard below.

    Scoped to the strings that are *sent*: `parse.py`'s system prompt goes to the
    model, and `shell.py`'s backticks are JavaScript template literals.
    """
    import ast
    from pathlib import Path

    offenders = []
    for path in sorted(Path("kanakko").rglob("*.py")):
        if path.name in ("parse.py", "shell.py"):
            continue
        tree = ast.parse(path.read_text())
        docstrings = {
            node.body[0].value.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "`" in node.value
                and node.lineno not in docstrings
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert offenders == [], f"literal backticks reach the user from: {offenders}"


def test_help_names_the_two_things_the_flat_list_never_did():
    """The old help listed commands only, so the flagship feature was invisible:
    nothing said a transfer is something you *type*, and nothing said an entry
    older than the last one is edited by tapping its row (§5's whole answer to
    having no field editor). Both are capabilities, not commands, which is
    exactly why a command list lost them."""
    assert "put 5000 in SIP" in handlers.HELP_TEXT
    assert "paid the credit card bill" in handlers.HELP_TEXT
    assert "Tap any row in Dashboard" in handlers.HELP_TEXT
    # /invite_signup stays out: operator-only (§16).
    assert "/invite_signup" not in handlers.HELP_TEXT


def test_help_text_is_html_telegram_will_accept():
    """`HELP_TEXT` is the one message sent with `parse_mode="HTML"`, and a markup
    mistake there fails *harder* than the plain-text problems above: Telegram
    answers 400 and the user gets **nothing at all** rather than an ugly
    character. So this parses it the way Telegram will.

    Three rules, each with a way it has nearly gone wrong: every `&` must open a
    real entity (`Bills & Utilities` had to become `Bills &amp; Utilities`); every
    `<` must open a tag Telegram supports, which is why the `<name>`/`<amount>`
    placeholders were replaced with concrete examples rather than escaped; and
    the tags must balance.
    """
    import re
    from html.parser import HTMLParser

    # Telegram's supported subset, minus the ones this text has no use for.
    ALLOWED = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a"}

    class Check(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in ALLOWED:
                self.bad.append(f"unsupported tag <{tag}>")
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                self.bad.append(f"unbalanced </{tag}>")

    parser = Check()
    parser.feed(handlers.HELP_TEXT)
    assert parser.bad == [], parser.bad
    assert parser.stack == [], f"unclosed tags: {parser.stack}"

    # A bare `&` is the easiest one to reintroduce and Telegram rejects it.
    bare = re.findall(r"&(?!(?:amp|lt|gt|quot|#\d+);)", handlers.HELP_TEXT)
    assert bare == [], "unescaped & in HELP_TEXT — Telegram will 400 the send"

    # Commands must stay bare so Telegram auto-links them; <code> would buy a
    # monospace font and lose the tap.
    assert "<code>" not in handlers.HELP_TEXT
