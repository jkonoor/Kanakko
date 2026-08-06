"""OpenRouter parse call (DECISIONS §2).

One LLM call per inbound message. The model returns a structured transaction via
`response_format: {type: "json_schema", ...}`, and `require_parameters: true` in
the provider preferences forces OpenRouter to route only to providers that honour
the schema parameters. The schema's `category` enum is generated from
`categories.py`, so the model can never invent a category.

`amount` is a **string** in the schema, not a number: JSON numbers decode to
`float` and float loses paise (DECISIONS §9). Keeping it a string means
`json.loads` yields a `str` that `money.parse_amount` can turn into an exact
`Decimal`.

This module only makes the call and returns the model's parsed JSON. Pydantic
validation and the single retry (§2's price of the provider abstraction) live in
the caller — that is a separate task.
"""

import json
import os

import httpx

from kanakko.categories import schema_enum

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# §2: default model is claude-opus-5. `or` (not get's default) because compose
# sets OPENROUTER_MODEL="" when unset, so get("...", default) would return "".
MODEL_DEFAULT = "claude-opus-5"

_SYSTEM_PROMPT = (
    "You extract a single personal-finance transaction from a short "
    "natural-language message. `type` is \"expense\" or \"income\". `amount` is "
    "the numeric value as a string, no currency sign or commas. `category` is "
    "the best-fitting category from the allowed set. `date` is the transaction "
    "date as YYYY-MM-DD. `note` preserves the user's original wording."
)


def parse_schema() -> dict:
    """The JSON schema handed to the model for one transaction (§2, §3)."""
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["expense", "income"]},
            "amount": {
                "type": "string",
                "description": "Numeric amount as a string, e.g. \"500.00\".",
            },
            "category": {"type": "string", "enum": schema_enum()},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "note": {"type": "string", "description": "The original wording."},
        },
        "required": ["type", "amount", "category", "date", "note"],
        "additionalProperties": False,
    }


def build_request(message: str, model: str | None = None) -> dict:
    """The OpenRouter request body for parsing `message`."""
    return {
        "model": model or os.environ.get("OPENROUTER_MODEL") or MODEL_DEFAULT,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "transaction",
                "strict": True,
                "schema": parse_schema(),
            },
        },
        "provider": {"require_parameters": True},
    }


def call(message: str) -> dict:
    """Send `message` to OpenRouter and return the model's parsed JSON.

    Raises `RuntimeError` if `OPENROUTER_API_KEY` is unset. Network and HTTP
    errors propagate as `httpx` exceptions.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    response = httpx.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=build_request(message),
        timeout=30.0,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content)
