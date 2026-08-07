"""SIGNUP_MODE fails closed (DECISIONS §16).

The whole point of the env var is that going public is deliberate. A reader that
treated `Open`, `true`, or a stray space as "open" would leak the bot to strangers
on the operator's OpenRouter credits — the exact bill §16 exists to stop. So the
guard here is the fail-closed direction: everything that is not literally `open`
must stay `invite`.
"""

import pytest

from kanakko.auth import signup_mode


def test_unset_is_invite(monkeypatch):
    monkeypatch.delenv("SIGNUP_MODE", raising=False)
    assert signup_mode() == "invite"


def test_open_opens_signup(monkeypatch):
    monkeypatch.setenv("SIGNUP_MODE", "open")
    assert signup_mode() == "open"


@pytest.mark.parametrize("value", ["", " ", "invite", "Open", "OPEN", "open ", "true", "yes"])
def test_anything_but_exactly_open_stays_closed(monkeypatch, value):
    monkeypatch.setenv("SIGNUP_MODE", value)
    assert signup_mode() == "invite"
