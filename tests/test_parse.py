"""The OpenRouter request body: schema, provider routing, model default.

No network here — `call()` is one thin httpx POST. What can break silently is
the *request body*: a wrong provider preference (schema ignored), a category
enum that drifts from `categories.py` (invented categories), an `amount` typed
as a number (json decodes it to float, paise gone — DECISIONS §9), or the model
default swallowed by compose's empty-string env var.
"""

import os

from kanakko.categories import schema_enum
from kanakko.parse import MODEL_DEFAULT, build_request, parse_schema


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
