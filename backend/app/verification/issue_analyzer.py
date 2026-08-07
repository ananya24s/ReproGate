"""Turns raw issue text into the structured reproduction intent used downstream.

The LLM's output is probabilistic; everything around it is not. The prompt is
versioned, the requested shape is a fixed schema, parsing is deterministic, and
the reply is validated before it leaves this module. A reply that does not
validate produces an error rather than a partially trusted result.

This module talks to :class:`~app.llm.provider.LLMProvider` only. It must never
import a vendor SDK.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMResponseError
from app.core.logging import get_logger
from app.llm.prompts import issue_analysis as prompt
from app.llm.provider import LLMProvider
from app.llm.schemas import LLMCompletionRequest, extract_json_object
from app.schemas.github import GitHubIssue
from app.schemas.issue_analysis import (
    EvidenceBasis,
    IssueAnalysis,
    IssueAnalysisPayload,
    ReproductionStep,
)

logger = get_logger(__name__)

#: The shape asked of the model, derived from the model that validates it, so
#: the two can never drift apart.
RESPONSE_SCHEMA: Final = IssueAnalysisPayload.model_json_schema()


class IssueAnalyzer:
    """Extracts a structured report from an issue's prose."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def analyze(
        self, issue: GitHubIssue, *, repository: str | None = None
    ) -> IssueAnalysis:
        """Analyze ``issue`` and return its structured representation.

        Args:
            issue: The normalized issue from ``app.github``.
            repository: ``owner/name``, included in the prompt for context.

        Returns:
            The structured analysis, tagged with the prompt version, provider,
            and model that produced it.

        Raises:
            LLMResponseError: The reply was unparseable or failed validation.
            LLMError: The provider could not be reached.
        """
        started = time.monotonic()

        messages = prompt.build_messages(
            schema=RESPONSE_SCHEMA,
            repository=repository or "",
            number=issue.number,
            state=issue.state.value,
            labels=issue.labels,
            title=issue.title,
            body=issue.body,
            body_char_limit=self._settings.issue_analysis_body_char_limit,
        )

        logger.info(
            "Analyzing issue",
            extra={
                "issue_number": issue.number,
                "repository": repository,
                "prompt_version": prompt.PROMPT_ID,
                "llm_provider": self._provider.name,
                "llm_model": self._provider.model,
                "has_body": bool(issue.body and issue.body.strip()),
            },
        )

        completion = await self._provider.complete(
            LLMCompletionRequest(
                messages=messages,
                temperature=self._settings.llm_temperature,
                max_output_tokens=self._settings.llm_max_output_tokens,
                expects_json=True,
                response_schema=RESPONSE_SCHEMA,
                schema_name=prompt.PROMPT_NAME,
            )
        )

        payload = _validate(completion.text, issue.number)
        steps, warnings = _enforce_stated_steps(payload.reproduction_steps)

        analysis = IssueAnalysis(
            issue_number=issue.number,
            summary=payload.summary,
            expected_behavior=payload.expected_behavior,
            observed_behavior=payload.observed_behavior,
            reproduction_steps=steps,
            environment=payload.environment,
            mentioned_entities=payload.mentioned_entities,
            prerequisites=payload.prerequisites,
            configuration_indicators=payload.configuration_indicators,
            stale_or_fixed_indicators=payload.stale_or_fixed_indicators,
            ambiguities=payload.ambiguities,
            missing_information=payload.missing_information,
            reproducibility=payload.reproducibility,
            prompt_version=prompt.PROMPT_ID,
            llm_provider=completion.provider,
            llm_model=completion.model,
            warnings=warnings,
            analyzed_at=datetime.now(tz=UTC),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        logger.info(
            "Analyzed issue",
            extra={
                "issue_number": issue.number,
                "prompt_version": analysis.prompt_version,
                "llm_model": analysis.llm_model,
                "reproduction_step_count": len(analysis.reproduction_steps),
                "mentioned_entity_count": len(analysis.mentioned_entities),
                "ambiguity_count": len(analysis.ambiguities),
                "missing_information_count": len(analysis.missing_information),
                "sufficient_for_reproduction": (
                    analysis.reproducibility.sufficient_for_reproduction
                ),
                "assessment_confidence": analysis.reproducibility.confidence.value,
                "warning_count": len(analysis.warnings),
                "duration_ms": analysis.duration_ms,
            },
        )
        return analysis


def _validate(text: str, issue_number: int) -> IssueAnalysisPayload:
    """Parse and validate a reply, refusing anything that does not fit."""
    payload = extract_json_object(text)

    try:
        return IssueAnalysisPayload.model_validate(payload)
    except PydanticValidationError as exc:
        # The individual errors name the offending fields, which is what makes
        # a bad reply diagnosable rather than merely rejected.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        logger.warning(
            "Issue analysis reply failed validation",
            extra={"issue_number": issue_number, "problems": problems},
        )
        raise LLMResponseError(
            f"The issue analysis reply did not match the expected schema: {problems}"
        ) from exc


def _enforce_stated_steps(
    steps: tuple[ReproductionStep, ...],
) -> tuple[tuple[ReproductionStep, ...], tuple[str, ...]]:
    """Discard any reproduction step the model inferred rather than read.

    An invented step would be executed against a real repository, so it is
    dropped and the removal is recorded rather than trusted.
    """
    stated = tuple(step for step in steps if step.basis is EvidenceBasis.STATED)
    discarded = len(steps) - len(stated)

    if discarded == 0:
        return stated, ()

    warning = (
        f"Discarded {discarded} reproduction step(s) the model inferred rather "
        "than read from the issue."
    )
    logger.warning(
        "Discarded inferred reproduction steps",
        extra={"discarded_count": discarded, "kept_count": len(stated)},
    )
    # Renumber so the surviving steps stay contiguous and ordered.
    renumbered = tuple(
        step.model_copy(update={"order": position})
        for position, step in enumerate(
            sorted(stated, key=lambda item: item.order), start=1
        )
    )
    return renumbered, (warning,)
