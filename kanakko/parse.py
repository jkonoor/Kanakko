"""OpenRouter parse call (DECISIONS §2).

One LLM call per inbound message. The model returns a structured transaction via
`response_format: {type: "json_schema", ...}`, and `require_parameters: true` in
the provider preferences forces OpenRouter to route only to providers that honour
the schema parameters. The schema's `category` enum is generated from
`categories.py`, so the model can never invent a category. The `account` enum
(§18) is the one departure from that pattern — it is per household, so it is
built per request from the caller's own accounts rather than a module-level
constant, and validated the same way through Pydantic context instead of a
class-level set.

`amount` is a **string** in the schema, not a number: JSON numbers decode to
`float` and float loses paise (DECISIONS §9). Keeping it a string means
`json.loads` yields a `str` that `money.parse_amount` can turn into an exact
`Decimal`.

`call()` makes the raw request; `parse_message()` validates the result with
Pydantic and retries exactly once on a schema failure — §2's price of the
provider abstraction, since OpenRouter's strict mode is best-effort per provider.
"""

import json
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

from kanakko.categories import ALL_CATEGORIES, schema_enum
from kanakko.money import parse_amount

# §10: one function returns the timezone; a per-user column replaces the constant
# later without touching call sites.
KOLKATA = ZoneInfo("Asia/Kolkata")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# §2: default model, revised 2026-08-07 from claude-opus-5 on a measured
# comparison — same 8/8 extraction accuracy across three runs, ~3x faster and
# ~12.5x cheaper, and it invented nothing on the must-not-parse cases. The table
# is in §2. Namespaced id: OpenRouter aliases some bare slugs but not all, and
# the namespaced form is the one its model list actually publishes.
# `or` (not get's default) because compose sets OPENROUTER_MODEL="" when unset,
# so get("...", default) would return "".
MODEL_DEFAULT = "google/gemini-2.5-flash"

_SYSTEM_PROMPT = (
    "You extract a single personal-finance transaction from a short "
    "natural-language message. `type` is \"expense\" or \"income\". `amount` is "
    "the numeric value as a string, no currency sign or commas. `category` is "
    "the best-fitting category from the allowed set. `date` is the transaction "
    "date as YYYY-MM-DD. `note` preserves the user's original wording."
)


def parse_schema(accounts: list[str] | None = None) -> dict:
    """The JSON schema handed to the model for one transaction (§2, §3).

    `accounts` is the caller's household's account names, built per request from
    the accounts table (§18) — never a literal, the same rule §11 applies to
    categories. Omitted (or a household with nothing to offer, e.g. a user with
    no household yet) leaves the schema exactly as it was before accounts
    existed: no `account` property, no behaviour change, no added prompt cost.
    """
    schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["expense", "income"]},
            "amount": {
                "type": "string",
                "description": "Numeric amount as a string, e.g. \"500.00\".",
            },
            # §3: category is nullable — the model returns null when it genuinely
            # cannot tell, and dispatch shows the buttons. amount is NOT nullable
            # (no amount → no transaction). Strict mode keeps every key in
            # `required`; optionality is expressed by the `anyOf` below.
            #
            # `anyOf`, not `{"type": ["string", "null"], "enum": [...]}`: Anthropic's
            # structured-output validator rejects a type *array* alongside an enum
            # with `Invalid schema: Enum value 'Food' does not match declared type
            # '['string', 'null']'` — a 400 from every provider OpenRouter tried
            # (Azure, Bedrock), so it is the schema, not one provider. Putting the
            # null in the enum instead (`{"type": "string", "enum": [..., None]}`)
            # fails the mirror-image way: `Enum value None does not match`. Verified
            # against the live API, 2026-08-07.
            "category": {
                "anyOf": [{"type": "string", "enum": schema_enum()}, {"type": "null"}]
            },
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "note": {"type": "string", "description": "The original wording."},
        },
        "required": ["type", "amount", "category", "date", "note"],
        "additionalProperties": False,
    }
    if accounts:
        # §18: nullable like category — null means "the default account", so the
        # model is never forced to guess when the message names no account.
        schema["properties"]["account"] = {
            "anyOf": [{"type": "string", "enum": list(accounts)}, {"type": "null"}]
        }
        schema["required"].append("account")
    return schema


def today() -> str:
    """Current date in `Asia/Kolkata` as YYYY-MM-DD (§10)."""
    return datetime.now(KOLKATA).strftime("%Y-%m-%d")


def resolve_model(model: str | None = None) -> str:
    """The model id an outbound parse request will use (§2).

    One definition: `build_request` sends it and §17's `parse.completed` log line
    reports it, so a slow model is greppable. `or` (not get's default) because
    compose sets `OPENROUTER_MODEL=""` when unset.
    """
    return model or os.environ.get("OPENROUTER_MODEL") or MODEL_DEFAULT


def build_request(
    message: str,
    model: str | None = None,
    today_str: str | None = None,
    accounts: list[str] | None = None,
) -> dict:
    """The OpenRouter request body for parsing `message`.

    §10: today's `Asia/Kolkata` date is injected into the system prompt so the
    model can resolve "yesterday"/"last Friday" instead of guessing. `today_str`
    is injectable for deterministic tests; production reads the wall clock.
    `accounts` is threaded straight into `parse_schema` (§18).
    """
    system = (
        f"{_SYSTEM_PROMPT} Today's date is {today_str or today()} "
        "(Asia/Kolkata). Resolve any relative date in the message "
        "(\"yesterday\", \"last Friday\") against it."
    )
    return {
        "model": resolve_model(model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "transaction",
                "strict": True,
                "schema": parse_schema(accounts),
            },
        },
        "provider": {"require_parameters": True},
    }


class Transaction(BaseModel):
    """A validated parse result (§2, §3).

    OpenRouter's strict mode is best-effort per provider, so the model's JSON is
    re-checked here rather than trusted. `amount` is routed through
    `money.parse_amount` — the single door every amount enters by (§9) — so a
    non-numeric, non-positive, `float`, or over-`NUMERIC(12,2)` value fails
    validation and triggers the retry. `extra="forbid"` mirrors the schema's
    `additionalProperties: false`.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["expense", "income"]
    amount: Decimal
    category: str | None  # §3: null when the model cannot tell → show buttons
    date: date
    note: str
    # §18: null means "the default account" — nullable for the same reason as
    # category. No closed set at class level, unlike category: the account list
    # is per household, not a module-level constant, so it can only be checked
    # against the caller's own accounts, passed in as validation context.
    account: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_is_exact(cls, value: object) -> Decimal:
        # A JSON number arrives as float and parse_amount raises TypeError, which
        # Pydantic does NOT wrap — re-raise as ValueError so a float amount surfaces
        # as a ValidationError (retried), never silently coerced to Decimal (§9).
        try:
            return parse_amount(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("category")
    @classmethod
    def _category_is_known(cls, value: str | None) -> str | None:
        # §3: null is a valid answer ("can't tell"); a non-null value must still
        # be one of the closed set so the model can't invent a category (§11).
        if value is not None and value not in ALL_CATEGORIES:
            raise ValueError(f"unknown category: {value!r}")
        return value

    @field_validator("account")
    @classmethod
    def _account_is_known(cls, value: str | None, info: ValidationInfo) -> str | None:
        # §18: the model must not invent an account any more than a category
        # (§11) — but the closed set is per household, so it travels as
        # validation context rather than a class-level constant. No context (or
        # no accounts in it) skips the check: today's tests and callers that
        # never pass `accounts` keep working unchanged.
        accounts = (info.context or {}).get("accounts") if info.context else None
        if accounts and value is not None and value not in accounts:
            raise ValueError(f"unknown account: {value!r}")
        return value


def call(message: str, accounts: list[str] | None = None) -> dict:
    """Send `message` to OpenRouter and return the model's parsed JSON.

    Raises `RuntimeError` if `OPENROUTER_API_KEY` is unset. Network and HTTP
    errors propagate as `httpx` exceptions. `accounts` is threaded straight into
    `build_request` (§18).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    response = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=build_request(message, accounts=accounts),
        timeout=30.0,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def parse_message(message: str, accounts: list[str] | None = None) -> Transaction:
    """Parse `message` into a validated `Transaction`, retrying once (§2).

    OpenRouter does not guarantee schema compliance, so the model's JSON is
    validated with Pydantic. On a schema failure — a `ValidationError` or
    non-JSON content — the call is retried exactly once; a second failure
    propagates. Network/HTTP errors and a missing key are not schema failures and
    are not retried. `accounts` (§18) is the caller's household's account names —
    threaded into the request schema and passed to Pydantic as context so a
    returned account name is checked against that same closed set.
    """
    try:
        return Transaction.model_validate(
            call(message, accounts), context={"accounts": accounts}
        )
    except (ValidationError, json.JSONDecodeError):
        return Transaction.model_validate(
            call(message, accounts), context={"accounts": accounts}
        )
