"""Telegram send client — no network.

`tg` is a thin httpx POST per Bot API method. What can break silently: sending
before a token is set (leaks nothing but 500s the handler), and handing the Bot
API a python-telegram-bot `InlineKeyboardMarkup` object it can't serialise
instead of the plain dict it expects.
"""

import httpx
import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kanakko import tg


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture(monkeypatch):
    """Replace httpx.post with a spy that records url+json and returns ok."""
    calls = []

    def fake_post(url, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(tg.httpx, "post", fake_post)
    return calls


def test_fails_closed_without_a_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # No httpx.post spy: a leak would try the network and fail differently.
    monkeypatch.setattr(
        tg.httpx,
        "post",
        lambda *a, **k: pytest.fail("posted with no token set"),
    )
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN is not set"):
        tg.send_message(123, "hi")


def test_url_carries_the_token_and_method(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    calls = _capture(monkeypatch)
    tg.answer_callback_query("cbq-1", text="Saved")
    assert calls[0]["url"] == "https://api.telegram.org/botT0KEN/answerCallbackQuery"
    assert calls[0]["json"] == {"callback_query_id": "cbq-1", "text": "Saved"}


def test_answer_omits_text_when_none(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    calls = _capture(monkeypatch)
    tg.answer_callback_query("cbq-1")
    assert calls[0]["json"] == {"callback_query_id": "cbq-1"}


def test_keyboard_is_serialised_to_a_plain_dict(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    calls = _capture(monkeypatch)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="ok")]])
    tg.send_message(42, "card", reply_markup=kb)
    sent = calls[0]["json"]["reply_markup"]
    # A raw InlineKeyboardMarkup is not JSON-serialisable by httpx; it must be
    # the Bot API's nested-list dict.
    assert isinstance(sent, dict)
    assert sent == kb.to_dict()
    assert sent["inline_keyboard"][0][0]["callback_data"] == "ok"


def test_edit_message_targets_the_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    calls = _capture(monkeypatch)
    tg.edit_message_text(42, 777, "done")
    assert calls[0]["url"].endswith("/editMessageText")
    assert calls[0]["json"] == {"chat_id": 42, "message_id": 777, "text": "done"}


def test_http_error_propagates(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")

    def boom(url, json, timeout):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(tg.httpx, "post", boom)
    with pytest.raises(httpx.HTTPError):
        tg.send_message(1, "x")


class _StatusResponse:
    """An httpx-like response whose raise_for_status raises HTTPStatusError."""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        raise httpx.HTTPStatusError(
            "error", request=httpx.Request("POST", "https://x"), response=self
        )


def test_edit_swallows_message_not_modified(monkeypatch):
    # Re-tapping the already-selected category re-renders identical content;
    # Telegram answers 400 "message is not modified". That must NOT propagate —
    # a raise here 500s the webhook and Telegram redelivers the tap forever.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    payload = {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}
    monkeypatch.setattr(tg.httpx, "post", lambda url, json, timeout: _StatusResponse(400, payload))
    assert tg.edit_message_text(1, 2, "same") == payload


def test_edit_still_raises_on_other_400(monkeypatch):
    # A different 400 (a real error) must still propagate — only the no-op edit
    # is swallowed, not every 400.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T0KEN")
    payload = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    monkeypatch.setattr(tg.httpx, "post", lambda url, json, timeout: _StatusResponse(400, payload))
    with pytest.raises(httpx.HTTPStatusError):
        tg.edit_message_text(1, 2, "x")
