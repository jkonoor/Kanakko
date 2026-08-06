"""The OpenRouter request body: schema, provider routing, model default.

No network here — `call()` is one thin httpx POST. What can break silently is
the *request body*: a wrong provider preference (schema ignored), a category
enum that drifts from `categories.py` (invented categories), an `amount` typed
as a number (json decodes it to float, paise gone — DECISIONS §9), or the model
default swallowed by compose's empty-string env var.
"""

import os
import re
from datetime import datetime, timezone

from kanakko import parse
from kanakko.categories import schema_enum
from kanakko.parse import MODEL_DEFAULT, build_request, parse_schema, today


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
