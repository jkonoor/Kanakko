"""Mini App `initData` HMAC validation (§13) — no network.

What breaks silently if this is wrong: a forged or tampered `initData` is
accepted, and anyone can open the dashboard as any user. So the guards assert
the *effect* — a good payload verifies and returns its user, a byte-flipped one
and a re-signed-with-the-wrong-token one both raise.
"""

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from kanakko.webapp import InitDataError, validate_init_data

TOKEN = "123456:AA-test-token"


def _sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Build a valid `initData` string for `fields`, signed with `token`."""
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


FIELDS = {"auth_date": "1700000000", "user": '{"id":42,"first_name":"Ann"}'}


def test_valid_init_data_verifies_and_returns_fields(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    fields = validate_init_data(_sign(FIELDS))
    assert fields["auth_date"] == "1700000000"
    assert fields["user"] == '{"id":42,"first_name":"Ann"}'
    assert "hash" not in fields


def test_tampered_field_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    init_data = _sign(FIELDS)
    # Swap the signed user id for a different one, keeping the original hash.
    forged = init_data.replace("%3A42%2C", "%3A99%2C")
    assert forged != init_data
    with pytest.raises(InitDataError):
        validate_init_data(forged)


def test_signature_from_a_different_token_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    forged = _sign(FIELDS, token="999999:attacker-token")
    with pytest.raises(InitDataError):
        validate_init_data(forged)


def test_signature_field_stays_in_the_data_check_string(monkeypatch):
    """A Bot API 8.0+ payload carrying `signature` must still verify (§13).

    `signature` is Telegram's *separate* Ed25519 signature, for third parties
    validating without the bot token. Only that path excludes it: Telegram
    computes the bot-token HMAC over `signature` like any other field, so
    dropping it here would reject every real client that sends one.

    Verified against the official SDK, which has both paths in one file
    (Telegram-Mini-Apps/telegram-apps, `packages/init-data-node/src/validation.ts`):
    the Ed25519 path skips `hash` *and* `signature` (L94-100), the bot-token HMAC
    path skips only `hash` (L251-264). The docs state the exclusion for the
    Ed25519 path alone ("except _hash_ and _signature_") and say nothing about it
    for the HMAC path — which is exactly why a reviewer talked themselves into
    `fields.pop("signature")` here once. This test is what makes that fail.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    signed = {**FIELDS, "signature": "Ed25519_sig_from_telegram"}
    fields = validate_init_data(_sign(signed))
    assert fields["signature"] == "Ed25519_sig_from_telegram"


def test_missing_hash_is_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(urlencode(FIELDS))


def test_fails_closed_without_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        validate_init_data(_sign(FIELDS))
