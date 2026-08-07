"""Structured request and response payloads exchanged with LLM providers.

These types are provider-independent. Nothing here mentions a vendor, an SDK,
or a wire format; each provider translates to and from its own API.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import LLMResponseError

JsonObject = dict[str, Any]

#: ```json … ``` and bare ``` … ``` wrappers models often add around JSON.
_CODE_FENCE_RE: Final = re.compile(
    r"^\s*```[A-Za-z0-9_-]*\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
)


class LLMRole(str, Enum):
    """Who authored a message in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    """One message sent to, or received from, a provider."""

    model_config = ConfigDict(frozen=True)

    role: LLMRole
    content: str


class LLMCompletionRequest(BaseModel):
    """A request for a single completion."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[LLMMessage, ...] = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    expects_json: bool = False
    """Ask the provider to constrain output to a JSON object where supported."""

    response_schema: JsonObject | None = None
    """The JSON Schema the reply should satisfy. Advisory: the caller always
    validates the parsed reply itself."""

    schema_name: str | None = None


class LLMUsage(BaseModel):
    """Token accounting reported by a provider."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class LLMCompletion(BaseModel):
    """A provider's reply, normalized."""

    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str
    usage: LLMUsage | None = None
    finish_reason: str | None = None
    duration_ms: int = Field(default=0, ge=0)


def extract_json_object(text: str) -> JsonObject:
    """Parse the JSON object out of a model reply.

    Tolerates the two things models reliably do wrong — wrapping JSON in a
    markdown fence, and adding prose around it — but nothing more. Parsing is
    deterministic: the same text always yields the same object or the same
    error.

    Raises:
        LLMResponseError: No JSON object could be recovered.
    """
    if not text or not text.strip():
        raise LLMResponseError("The LLM returned an empty response.")

    candidate = text.strip()

    fenced = _CODE_FENCE_RE.match(candidate)
    if fenced is not None:
        candidate = fenced.group("body").strip()

    payload = _loads_object(candidate)
    if payload is not None:
        return payload

    # Fall back to the outermost braces, which recovers a reply wrapped in
    # explanatory prose.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        payload = _loads_object(candidate[start : end + 1])
        if payload is not None:
            return payload

    raise LLMResponseError("The LLM response did not contain a JSON object.")


def _loads_object(text: str) -> JsonObject | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
