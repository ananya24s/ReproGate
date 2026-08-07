"""The abstract LLM provider interface that every concrete provider implements.

Verification modules depend on this interface only. Swapping providers must
never require a change outside ``app.llm``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType

from app.llm.schemas import LLMCompletion, LLMCompletionRequest


class LLMProvider(ABC):
    """A source of text completions."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider identifier, recorded against every verification run."""

    @property
    @abstractmethod
    def model(self) -> str:
        """The model this instance sends requests to."""

    @abstractmethod
    async def complete(self, request: LLMCompletionRequest) -> LLMCompletion:
        """Produce a single completion.

        Raises:
            LLMError: The provider could not be reached or refused the request.
        """

    async def aclose(self) -> None:  # noqa: B027
        """Release any provider-held resources. Safe to call more than once.

        Deliberately concrete and empty: a provider holding no resources should
        not be forced to implement a no-op.
        """

    async def __aenter__(self) -> LLMProvider:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
