"""OpenAI implementation of the LLM provider interface.

This is the only module permitted to import the OpenAI SDK.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError, LLMError
from app.core.logging import get_logger
from app.llm.provider import LLMProvider
from app.llm.schemas import LLMCompletion, LLMCompletionRequest, LLMUsage

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """Completions backed by the OpenAI Chat Completions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.openai_api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not set; the OpenAI provider cannot be used."
            )

        # Imported lazily so that neither the SDK nor a configured key is
        # needed to import this module — tests use a fake provider.
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=self._settings.openai_api_key,
            timeout=float(self._settings.llm_request_timeout_seconds),
            max_retries=self._settings.llm_max_retries,
        )

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._settings.llm_model

    async def complete(self, request: LLMCompletionRequest) -> LLMCompletion:
        """Send ``request`` to OpenAI and normalize the reply."""
        from openai import OpenAIError

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": request.temperature,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.expects_json:
            # JSON mode rather than a strict json_schema: it is supported by
            # far more models, and the caller validates the reply anyway.
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**payload)
        except OpenAIError as exc:
            logger.warning(
                "OpenAI request failed",
                extra={"model": self.model, "error_type": type(exc).__name__},
            )
            raise LLMError(f"The OpenAI request failed: {exc}") from exc

        duration_ms = int((time.monotonic() - started) * 1000)

        if not response.choices:
            raise LLMError("OpenAI returned no completion choices.")

        choice = response.choices[0]
        usage = response.usage
        completion = LLMCompletion(
            text=choice.message.content or "",
            provider=self.name,
            model=response.model or self.model,
            finish_reason=choice.finish_reason,
            duration_ms=duration_ms,
            usage=(
                LLMUsage(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                )
                if usage is not None
                else None
            ),
        )

        logger.info(
            "Completed OpenAI request",
            extra={
                "model": completion.model,
                "finish_reason": completion.finish_reason,
                "total_tokens": completion.usage.total_tokens
                if completion.usage
                else None,
                "duration_ms": duration_ms,
            },
        )
        return completion

    async def aclose(self) -> None:
        await self._client.close()
