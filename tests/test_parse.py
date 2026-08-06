"""The OpenRouter request body: schema, provider routing, model default.

No network here — `call()` is one thin httpx POST. What can break silently is
the *request body*: a wrong provider preference (schema ignored), a category
enum that drifts from `categories.py` (invented categories), an `amount` typed
as a number (json decodes it to float, paise gone — DECISIONS §9), or the model
default swallowed by compose's empty-string env var.
"""

import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from kanakko import parse
from kanakko.categories import schema_enum
from kanakko.parse import (
    MODEL_DEFAULT,
    Transaction,
    build_request,
    parse_message,
    parse_schema,
    today,
)


def test_require_parameters_forces_schema_aware_provider():
    body = build_request("spent 500 on lunch")
    assert body["provider"]["require_parameters"] is True
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == parse_schema()


def test_category_enum_comes_from_categories_module():
    # The model must not be able to invent a category (§11).
    assert parse_schema()["properties"]["category"]["enum"] == schema_enum()


def test_amount_is_a_string_not_a_number():
    # A JSON number decodes to float, which loses paise. Amount stays a string
    # so json.loads yields a str that money.parse_amount can make exact (§9).
    assert parse_schema()["properties"]["amount"]["type"] == "string"


def test_model_default_survives_empty_env(monkeypatch):
    # Compose sets OPENROUTER_MODEL="" when the operator leaves it unset, so a
    # naive get("OPENROUTER_MODEL", default) would send "". §2 default must win.
    monkeypatch.setenv("OPENROUTER_MODEL", "")
    assert build_request("x")["model"] == MODEL_DEFAULT
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert build_request("x")["model"] == MODEL_DEFAULT
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
    assert build_request("x")["model"] == "anthropic/claude-sonnet-5"


def _system_content(body):
    return next(m["content"] for m in body["messages"] if m["role"] == "system")


def test_current_kolkata_date_is_injected_into_the_prompt():
    # §10: without today's date the model guesses "yesterday"/"last Friday".
    # The date must reach the system prompt verbatim.
    system = _system_content(build_request("spent 500 yesterday", today_str="2026-08-06"))
    assert "2026-08-06" in system


def test_today_reads_the_kolkata_clock(monkeypatch):
    # today() must resolve in Asia/Kolkata, not UTC. Pin an instant where the two
    # zones fall on different calendar days: 2026-08-06 20:00 UTC is already
    # 2026-08-07 01:30 IST. A naive-UTC today() would return 2026-08-06; the
    # Kolkata one returns 2026-08-07. This reddens the moment KOLKATA is wrong.
    fixed = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(parse, "datetime", FrozenDatetime)
    assert today() == "2026-08-07"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today())


# --- Pydantic validation + one retry (§2) -------------------------------------

_GOOD = {
    "type": "expense",
    "amount": "500.50",
    "category": "Food",
    "date": "2026-08-06",
    "note": "lunch",
}


def _feed(monkeypatch, *responses):
    """Replace parse.call with one that yields the given responses in order,
    recording each invocation so the retry count can be asserted."""
    calls = []

    def fake_call(message):
        calls.append(message)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(parse, "call", fake_call)
    return calls


def test_valid_response_validates_to_typed_transaction(monkeypatch):
    _feed(monkeypatch, _GOOD)
    txn = parse_message("spent 500.50 on lunch")
    assert isinstance(txn, Transaction)
    assert txn.type == "expense"
    assert txn.amount == Decimal("500.50")
    assert isinstance(txn.amount, Decimal)  # §9: never float
    assert txn.category == "Food"
    assert txn.date == date(2026, 8, 6)


def test_schema_failure_is_retried_exactly_once(monkeypatch):
    bad = {**_GOOD, "category": "Nonsense"}  # not in the closed set → invalid
    calls = _feed(monkeypatch, bad, _GOOD)
    txn = parse_message("x")
    assert txn.category == "Food"
    assert len(calls) == 2  # one retry after the first failure


def test_two_failures_raise_and_do_not_loop(monkeypatch):
    bad = {**_GOOD, "amount": "0"}  # parse_amount rejects non-positive
    calls = _feed(monkeypatch, bad, bad)
    with pytest.raises(ValidationError):
        parse_message("x")
    assert len(calls) == 2  # exactly one retry, then give up — not infinite


def test_float_amount_is_refused_not_coerced(monkeypatch):
    # A provider ignoring strict mode returns amount as a JSON number → float.
    # Pydantic would happily coerce float→Decimal (losing paise) without the
    # parse_amount validator; here it must fail and exhaust the single retry.
    bad = {**_GOOD, "amount": 500.5}
    calls = _feed(monkeypatch, bad, bad)
    with pytest.raises(ValidationError):
        parse_message("x")
    assert len(calls) == 2


def test_non_json_content_is_retried(monkeypatch):
    # A provider that ignores response_format may return prose, so call() raises
    # JSONDecodeError. That is a schema failure and must be retried, not raised.
    err = json.JSONDecodeError("nope", "not json", 0)
    calls = _feed(monkeypatch, err, _GOOD)
    txn = parse_message("x")
    assert txn.category == "Food"
    assert len(calls) == 2


def test_http_error_is_not_retried(monkeypatch):
    # A network/HTTP error is not a schema failure — it must propagate on the
    # first call without a retry.
    calls = _feed(monkeypatch, httpx.HTTPError("boom"), _GOOD)
    with pytest.raises(httpx.HTTPError):
        parse_message("x")
    assert len(calls) == 1
