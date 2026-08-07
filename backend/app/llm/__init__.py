"""Provider-independent LLM interface.

OpenAI is the initial provider, but verification modules must not depend
directly on the OpenAI SDK.
"""

from app.llm.provider import LLMProvider
from app.llm.schemas import (
    LLMCompletion,
    LLMCompletionRequest,
    LLMMessage,
    LLMRole,
    LLMUsage,
    extract_json_object,
)

__all__ = [
    "LLMCompletion",
    "LLMCompletionRequest",
    "LLMMessage",
    "LLMProvider",
    "LLMRole",
    "LLMUsage",
    "extract_json_object",
]
