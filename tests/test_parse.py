"""The OpenRouter request body: schema, provider routing, model default.

No network here — `call()` is one thin httpx POST. What can break silently is
the *request body*: a wrong provider preference (schema ignored), a category
enum that drifts from `categories.py` (invented categories), an `amount` typed
as a number (json decodes it to float, paise gone — DECISIONS §9), or the model
default swallowed by compose's empty-string env var.
"""

import json
import pathlib
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
    # The model must not be able to invent a category (§11); null is added by the
    # schema for §3 nullability, not by categories.py.
    cat = parse_schema()["properties"]["category"]
    assert {"type": "string", "enum": schema_enum()} in cat["anyOf"]
    assert {"type": "null"} in cat["anyOf"]  # §3: category is nullable
    # amount is NOT nullable — no amount means no transaction (§3).
    assert parse_schema()["properties"]["amount"]["type"] == "string"


def test_no_property_declares_a_type_array():
    """A JSON-Schema type *array* is rejected by Anthropic's structured-output
    validator, and it fails as a 400 from the provider — never locally (§2).

    This is the bug the previous version of the test above locked in. It asserted
    `cat["type"] == ["string", "null"]` and `cat["enum"] == [*schema_enum(), None]`
    — the exact shape the API rejects — so it stayed green through every run while
    *every* real parse returned 400 and the webhook 500'd. The ledger was empty for
    that reason, not because nothing had been logged.

    Neither form is reachable from a unit test, so this guards the construct rather
    than the response: no property may declare `type` as a list, and no enum may
    carry a `None` member. Both were verified against the live API on 2026-08-07 —
    `anyOf` returns 200, both rejected forms return 400.
    """
    for name, prop in parse_schema()["properties"].items():
        for branch in prop.get("anyOf", [prop]):
            assert not isinstance(branch.get("type"), list), f"{name} declares a type array"
            assert None not in branch.get("enum", []), f"{name} has None inside an enum"


def test_amount_is_a_string_not_a_number():
    # A JSON number decodes to float, which loses paise. Amount stays a string
    # so json.loads yields a str that money.parse_amount can make exact (§9).
    assert parse_schema()["properties"]["amount"]["type"] == "string"


def test_account_enum_is_absent_without_a_real_choice():
    # §18: no accounts, no accounts passed, or exactly one account must all leave
    # the schema exactly as it was before accounts existed — same prompt cost,
    # same behaviour, for a caller with nothing (or nothing to choose between).
    assert "account" not in parse_schema()["properties"]
    assert "account" not in parse_schema(accounts=[])["properties"]
    assert "account" not in parse_schema(accounts=["Bank"])["properties"]
    assert "account" not in parse_schema()["required"]


def test_account_enum_is_built_per_request_from_the_given_accounts():
    # §18's departure from §11: the account list is per household, assembled at
    # request time — never a module-level literal like categories.py.
    schema = parse_schema(accounts=["Bank", "Card"])
    account = schema["properties"]["account"]
    assert {"type": "string", "enum": ["Bank", "Card"]} in account["anyOf"]
    assert {"type": "null"} in account["anyOf"]  # §18: null → the default account
    assert "account" in schema["required"]  # strict mode keeps every key required


def test_build_request_threads_accounts_into_the_schema():
    body = build_request("swiped 500 on card", accounts=["Bank", "Card"])
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["account"]["anyOf"][0]["enum"] == ["Bank", "Card"]


def test_account_vocabulary_guidance_appears_only_with_a_real_account_choice():
    # §18: "swiped"/"on card" -> credit, "UPI"/"paid cash" -> the bank account
    # (never its own account — UPI is a rail, not a pool), "SIP"/"FD"/"chit" ->
    # the locked account. Only worth teaching once there's a real account to
    # route to — same gate as the enum itself, so a single-account household's
    # prompt is unchanged (no added cost).
    system = build_request("swiped 500 on card", accounts=["Bank", "Card"])[
        "messages"
    ][0]["content"]
    assert "swiped" in system
    assert "UPI" in system
    assert "SIP" in system

    one_account = build_request("swiped 500 on card", accounts=["Bank"])
    assert "swiped" not in one_account["messages"][0]["content"]

    no_accounts = build_request("swiped 500 on card")
    assert "swiped" not in no_accounts["messages"][0]["content"]


def test_a_household_with_one_account_behaves_like_no_accounts_at_all():
    # The check the task names explicitly: a single-account household has no
    # real choice to make (null already means "the default account"), so its
    # schema must be byte-for-byte today's (pre-accounts) schema — no `account`
    # property, no added prompt cost.
    assert parse_schema(accounts=["Bank"]) == parse_schema()


def test_account_enum_appears_only_once_a_second_account_exists():
    # The enum is real signal only once there is a real choice — §18:
    # "accounts become visible only when a second one exists".
    schema = parse_schema(accounts=["Bank", "Card"])
    account = schema["properties"]["account"]
    assert {"type": "string", "enum": ["Bank", "Card"]} in account["anyOf"]
    assert {"type": "null"} in account["anyOf"]
    assert "account" in schema["required"]


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

    def fake_call(message, accounts=None):
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


def test_null_category_validates_without_retry(monkeypatch):
    # §3: category null is a valid answer ("can't tell" → show buttons), not a
    # schema failure. It must validate on the first call, no retry.
    calls = _feed(monkeypatch, {**_GOOD, "category": None})
    txn = parse_message("paid 500")
    assert txn.category is None
    assert len(calls) == 1


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


def test_account_outside_the_offered_set_is_refused_not_retried(monkeypatch):
    # §18: the model must not invent an account any more than a category (§11).
    # The closed set is per request, so this is checked via validation context —
    # unlike category, there is no module-level constant to fall back on.
    bad = {**_GOOD, "account": "Nonexistent"}
    calls = _feed(monkeypatch, bad, bad)
    with pytest.raises(ValidationError):
        parse_message("x", accounts=["Bank", "Card"])
    assert len(calls) == 2  # exactly one retry, same as any other schema failure


def test_account_within_the_offered_set_validates(monkeypatch):
    calls = _feed(monkeypatch, {**_GOOD, "account": "Card"})
    txn = parse_message("swiped 500 on card", accounts=["Bank", "Card"])
    assert txn.account == "Card"
    assert len(calls) == 1


def test_null_account_validates_without_retry(monkeypatch):
    # §18: null means "the default account" — a valid answer, not a failure.
    calls = _feed(monkeypatch, {**_GOOD, "account": None})
    txn = parse_message("paid 500", accounts=["Bank", "Card"])
    assert txn.account is None
    assert len(calls) == 1


def test_an_account_name_is_not_checked_when_no_accounts_were_offered(monkeypatch):
    # Callers that never pass `accounts` (today's tests, or a user with no
    # household yet) get no validation surface — nothing to check against.
    calls = _feed(monkeypatch, {**_GOOD, "account": "Anything"})
    txn = parse_message("x")
    assert txn.account == "Anything"
    assert len(calls) == 1


def test_http_error_is_not_retried(monkeypatch):
    # A network/HTTP error is not a schema failure — it must propagate on the
    # first call without a retry.
    calls = _feed(monkeypatch, httpx.HTTPError("boom"), _GOOD)
    with pytest.raises(httpx.HTTPError):
        parse_message("x")
    assert len(calls) == 1


def test_spec_and_code_agree_on_the_default_model():
    """`docs/DECISIONS.md` §2 and `MODEL_DEFAULT` name the same model.

    CLAUDE.md: "Two places that must agree will eventually disagree." These two
    did — production ran `google/gemini-2.5-flash` via `OPENROUTER_MODEL` for a
    day while §2 still declared `claude-opus-5`, so the spec stopped describing
    what actually ran and no check noticed. The spec is the authority, so this
    reads the model out of it and holds the code to it; changing one without the
    other now fails here rather than silently.
    """
    spec = pathlib.Path(__file__).resolve().parent.parent / "docs" / "DECISIONS.md"
    declared = re.search(r"Default model \*\*`([^`]+)`\*\*", spec.read_text())
    assert declared, "§2 no longer declares a default model in the expected form"
    assert declared.group(1) == MODEL_DEFAULT
