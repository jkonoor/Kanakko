"""`/start` onboarding: invite consumption and household-of-one creation (§16).

`/start` is the bot's entry point and the one handler that runs *before* the
authorization gate — consuming an invite is how an unknown user becomes known, so
the gate cannot precede it. The check §16 names: a spent code, an expired code,
and a garbage payload are each refused **distinctly**, and **none creates a
partial household** — no user row, no household, no membership for a refused
sender (§16: nothing is stored). The valid paths prove the counter-direction: a
signup code opens a household of one, a household code joins the inviter's
household, and a redelivery is idempotent.
"""

from fastapi.testclient import TestClient

from kanakko import app as app_module
from kanakko import handlers
from kanakko.app import WEBHOOK_SECRET_HEADER, app
from kanakko.db import create_household_of_one, get_or_create_user
from kanakko.handlers import TextMessage, handle_start
from kanakko.migrate import migrate

OPERATOR_TG = 111
NEWUSER_TG = 999

SIGNUP_CODE = "signup-fresh-A1"
HOUSE_CODE = "house-fresh-B2"
SPENT_CODE = "signup-spent-C3"
EXPIRED_CODE = "signup-expired-D4"


def _seed(conn):
    """An operator with a household and four invites: fresh signup, fresh
    household, an already-spent signup, and an expired signup."""
    operator = get_or_create_user(conn, OPERATOR_TG)
    hid = create_household_of_one(conn, operator)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " VALUES (%s, 'signup', NULL, 'signup', %s)",
            (SIGNUP_CODE, operator),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by)"
            " VALUES (%s, 'household', %s, 'house', %s)",
            (HOUSE_CODE, hid, operator),
        )
        cur.execute(
            "INSERT INTO invites"
            " (code, kind, household_id, label, created_by, used_by, used_at)"
            " VALUES (%s, 'signup', NULL, 'spent', %s, %s, now())",
            (SPENT_CODE, operator, operator),
        )
        cur.execute(
            "INSERT INTO invites (code, kind, household_id, label, created_by, expires_at)"
            " VALUES (%s, 'signup', NULL, 'expired', %s, now() - interval '1 day')",
            (EXPIRED_CODE, operator),
        )
    return operator, hid


def _capture_send(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))
    return sent


def _footprint(conn, tg):
    """(user rows, memberships, total households) attributable to `tg` — a
    refused `/start` must leave all three untouched (§16 stores nothing)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = %s", (tg,))
        (users,) = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM household_members m JOIN users u"
            " ON u.user_id = m.user_id WHERE u.telegram_user_id = %s",
            (tg,),
        )
        (members,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM households")
        (households,) = cur.fetchone()
    return users, members, households


def _start(text, from_id=NEWUSER_TG):
    return TextMessage(chat_id=from_id, message_id=1, text=text,
                       from_id=from_id, update_id=1)


def test_payload_validator_enforces_the_64_char_charset():
    """§16's verified limit: A-Z a-z 0-9 _ -, up to 64 chars. This isolates the
    format guard — end to end an invalid payload and an unknown-but-well-formed
    code both refuse identically, so only a direct check reddens if the regex is
    loosened.
    """
    ok = handlers._START_PAYLOAD_RE.match
    assert ok("a" * 64) and not ok("a" * 65)  # the 64-char ceiling
    assert ok("Sign_up-Code9")  # the full allowed charset
    assert not ok("has space")
    assert not ok("bang!")
    assert not ok("")  # a bare /start is not a payload


def test_refusal_messages_are_distinct():
    """§16 says each refusal is distinct — a spent link, an expired link, and a
    payload that was never a link must not read identically."""
    assert len({handlers.START_SPENT, handlers.START_EXPIRED,
                handlers.START_BAD_CODE}) == 3


def test_signup_invite_opens_a_household_of_one(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start(f"/start {SIGNUP_CODE}"))
    assert outcome == "ok"
    assert sent == [(NEWUSER_TG, handlers.WELCOME)]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.owner FROM household_members m"
            " JOIN households h ON h.household_id = m.household_id"
            " JOIN users u ON u.user_id = m.user_id"
            " WHERE u.telegram_user_id = %s",
            (NEWUSER_TG,),
        )
        row = cur.fetchone()
        cur.execute("SELECT used_by FROM invites WHERE code = %s", (SIGNUP_CODE,))
        (used_by,) = cur.fetchone()
    assert row is not None  # a household exists, owned by the new user
    assert used_by is not None  # the code is now spent
    conn.rollback()


def test_household_invite_joins_the_inviters_household(conn, monkeypatch):
    migrate(conn)
    _operator, hid = _seed(conn)
    _capture_send(monkeypatch)
    outcome = handle_start(conn, _start(f"/start {HOUSE_CODE}"))
    assert outcome == "ok"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.household_id FROM household_members m"
            " JOIN users u ON u.user_id = m.user_id"
            " WHERE u.telegram_user_id = %s",
            (NEWUSER_TG,),
        )
        (joined,) = cur.fetchone()
    assert joined == hid  # joined the inviter's household, not a new one
    conn.rollback()


def test_spent_code_refused_and_stores_nothing(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    before = _footprint(conn, NEWUSER_TG)
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start(f"/start {SPENT_CODE}"))
    assert outcome == "spent"
    assert sent == [(NEWUSER_TG, handlers.START_SPENT)]
    assert _footprint(conn, NEWUSER_TG) == before == (0, 0, 1)  # no partial household
    conn.rollback()


def test_expired_code_refused_and_stores_nothing(conn, monkeypatch):
    migrate(conn)
    _seed(conn)
    before = _footprint(conn, NEWUSER_TG)
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start(f"/start {EXPIRED_CODE}"))
    assert outcome == "expired"
    assert sent == [(NEWUSER_TG, handlers.START_EXPIRED)]
    assert _footprint(conn, NEWUSER_TG) == before == (0, 0, 1)  # no partial household
    conn.rollback()


def test_garbage_payload_refused_and_stores_nothing(conn, monkeypatch):
    """A payload with spaces/punctuation is never a valid code — refused before
    any DB lookup, so no user row and no household are minted (§16)."""
    migrate(conn)
    _seed(conn)
    before = _footprint(conn, NEWUSER_TG)
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start("/start not a real code!!!"))
    assert outcome == "unknown"
    assert sent == [(NEWUSER_TG, handlers.START_BAD_CODE)]
    assert _footprint(conn, NEWUSER_TG) == before == (0, 0, 1)  # no partial household
    conn.rollback()


def test_unknown_code_refused(conn, monkeypatch):
    """A well-formed code that isn't in the table is refused, mints nothing."""
    migrate(conn)
    _seed(conn)
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start("/start nope-no-such-code"))
    assert outcome == "unknown"
    assert sent == [(NEWUSER_TG, handlers.START_BAD_CODE)]
    assert _footprint(conn, NEWUSER_TG) == (0, 0, 1)
    conn.rollback()


def test_redelivered_start_is_idempotent(conn, monkeypatch):
    """Telegram redelivers the same `/start <code>`; the second run must not
    refuse the now-admitted user, and must not double-home them."""
    migrate(conn)
    _seed(conn)
    _capture_send(monkeypatch)
    first = handle_start(conn, _start(f"/start {SIGNUP_CODE}"))
    second = handle_start(conn, _start(f"/start {SIGNUP_CODE}"))
    assert first == "ok" and second == "ok"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM household_members m JOIN users u"
            " ON u.user_id = m.user_id WHERE u.telegram_user_id = %s",
            (NEWUSER_TG,),
        )
        (members,) = cur.fetchone()
    assert members == 1  # one household, not two
    conn.rollback()


def test_bare_start_open_mode_opens_a_household(conn, monkeypatch):
    migrate(conn)
    monkeypatch.setenv("SIGNUP_MODE", "open")
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start("/start"))
    assert outcome == "ok"
    assert sent == [(NEWUSER_TG, handlers.WELCOME)]
    assert _footprint(conn, NEWUSER_TG)[:2] == (1, 1)  # user + membership
    conn.rollback()


def test_bare_start_invite_mode_unknown_user_is_turned_away(conn, monkeypatch):
    migrate(conn)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    sent = _capture_send(monkeypatch)
    outcome = handle_start(conn, _start("/start"))
    assert outcome == "refused"
    assert sent == [(NEWUSER_TG, handlers.ACCESS_REFUSED)]
    assert _footprint(conn, NEWUSER_TG) == (0, 0, 0)  # nothing stored
    conn.rollback()


# --- webhook integration: /start must bypass the gate --------------------------

client = TestClient(app)
SECRET = "s3cret-webhook-token_ABC"
AUTH = {WEBHOOK_SECRET_HEADER: SECRET}


def _reuse_conn(conn):
    class _Reuse:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    return lambda: _Reuse()


def test_start_with_signup_code_bypasses_the_gate_in_invite_mode(conn, monkeypatch):
    """The gate refuses unknown users in invite mode — but `/start <code>` is how
    they get in, so it must run before the gate. Reverting the pre-gate `/start`
    branch reddens this: `is_authorized` would refuse the sender before the invite
    could admit them.
    """
    migrate(conn)
    _seed(conn)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    monkeypatch.setattr(app_module, "connect", _reuse_conn(conn))
    sent = []
    monkeypatch.setattr(handlers, "send_message",
                        lambda chat_id, text: sent.append((chat_id, text)))

    update = {
        "update_id": 800,
        "message": {
            "message_id": 1,
            "chat": {"id": NEWUSER_TG},
            "from": {"id": NEWUSER_TG},
            "text": f"/start {SIGNUP_CODE}",
        },
    }
    response = client.post("/webhook", json=update, headers=AUTH)

    assert response.status_code == 200
    assert sent == [(NEWUSER_TG, handlers.WELCOME)]  # admitted, not refused
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE telegram_user_id = %s",
                    (NEWUSER_TG,))
        (users,) = cur.fetchone()
    assert users == 1  # the invite admitted them
    conn.rollback()
