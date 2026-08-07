"""Produces a candidate reproduction test from issue intent and repository context.

The model writes the test; this module decides whether the result is fit to
execute. Two checks run before any request is made, and every reply is checked
against the context it was given — in particular, an import the model invented
is rejected rather than sent to a sandbox.

This module talks to :class:`~app.llm.provider.LLMProvider` only. It must never
import a vendor SDK.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import LLMResponseError
from app.core.logging import get_logger
from app.llm.prompts import test_generation as prompt
from app.llm.provider import LLMProvider
from app.llm.schemas import LLMCompletionRequest, extract_json_object
from app.repository_analysis.models import TestFramework
from app.schemas.issue_analysis import Confidence
from app.schemas.test_generation import (
    GeneratedReproductionTest,
    GeneratedTestPayload,
    InsufficientContext,
    RequiredDependency,
    TestGenerationOutcome,
    TestGenerationPayload,
    TestGenerationResult,
    TestLanguage,
    VerificationContext,
)

logger = get_logger(__name__)

#: The shape asked of the model, derived from the model that validates it, so
#: the two can never drift apart.
RESPONSE_SCHEMA: Final = TestGenerationPayload.model_json_schema()

#: Frameworks a generated test can target.
SUPPORTED_FRAMEWORKS: Final = frozenset({TestFramework.JEST, TestFramework.VITEST})

_LANGUAGE_EXTENSIONS: Final[dict[TestLanguage, frozenset[str]]] = {
    TestLanguage.TYPESCRIPT: frozenset({".ts", ".tsx", ".mts", ".cts"}),
    TestLanguage.JAVASCRIPT: frozenset({".js", ".jsx", ".mjs", ".cjs"}),
}

#: Extensions tried when resolving an import against the context.
_MODULE_EXTENSIONS: Final = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
)

#: TypeScript ESM imports name the emitted `.js` file; map back to the source.
_EMITTED_TO_SOURCE: Final[dict[str, tuple[str, ...]]] = {
    ".js": (".ts", ".tsx"),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}

#: Relative specifiers in module positions only. Runtime paths a test builds
#: for itself — a temp fixture it writes and reads — are not module references
#: and must not be mistaken for invented files.
_MODULE_REFERENCE_RE: Final = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*"""
    r"""|\bvi\.mock\s*\(\s*|\bjest\.mock\s*\(\s*)"""
    r"""['"](\.{1,2}/[^'"\n]+)['"]"""
)

#: Filenames a framework will actually collect.
_DISCOVERY_MARKERS: Final = (".test.", ".spec.")


class ReproductionTestGenerator:
    """Writes a candidate reproduction test, or explains why it cannot."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: Settings | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    async def generate(self, context: VerificationContext) -> TestGenerationResult:
        """Generate a reproduction test from ``context``.

        Args:
            context: The complete set of facts generation may use. Nothing
                outside it is consulted.

        Returns:
            A result carrying either a candidate test or the reason one could
            not responsibly be written. Never raises for an unusable
            repository — that is an outcome, not an error.

        Raises:
            LLMResponseError: The reply was unparseable, failed validation, or
                referenced a file the context does not contain.
            LLMError: The provider could not be reached.
        """
        started = time.monotonic()
        analysis = context.repository_analysis
        issue_number = context.issue_analysis.issue_number

        framework = analysis.test_framework
        if framework not in SUPPORTED_FRAMEWORKS:
            return self._refuse(
                TestGenerationOutcome.UNSUPPORTED_FRAMEWORK,
                InsufficientContext(
                    reason=(
                        "No supported test framework was detected in the repository."
                        if framework is None
                        else f"{framework.value} is not a supported framework."
                    ),
                    missing=("a Jest or Vitest setup in the repository",),
                ),
                started=started,
                issue_number=issue_number,
            )

        if not context.has_context:
            return self._refuse(
                TestGenerationOutcome.INSUFFICIENT_CONTEXT,
                InsufficientContext(
                    reason="No repository files were supplied for this issue.",
                    missing=("at least one relevant source file",),
                ),
                started=started,
                issue_number=issue_number,
            )

        messages = prompt.build_messages(
            schema=RESPONSE_SCHEMA,
            repository=_repository_block(context),
            repository_analysis=_analysis_block(context),
            issue_analysis=_issue_block(context),
            context_files=self._render_context(context),
        )

        logger.info(
            "Generating reproduction test",
            extra={
                "issue_number": issue_number,
                "repository": context.repository.full_name,
                "framework": framework.value,
                "context_file_count": len(context.available_paths),
                "snippet_count": len(context.snippets),
                "prompt_version": prompt.PROMPT_ID,
                "llm_provider": self._provider.name,
                "llm_model": self._provider.model,
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

        payload = _validate(completion.text, issue_number)

        if payload.outcome == "insufficient_context":
            if payload.insufficient_context is None:  # pragma: no cover
                raise LLMResponseError(
                    "The reply declined to generate without saying why."
                )
            result = TestGenerationResult(
                outcome=TestGenerationOutcome.INSUFFICIENT_CONTEXT,
                insufficient_context=payload.insufficient_context,
                prompt_version=prompt.PROMPT_ID,
                llm_provider=completion.provider,
                llm_model=completion.model,
                generated_at=datetime.now(tz=UTC),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            _log_result(result, issue_number)
            return result

        if payload.test is None:  # pragma: no cover - the validator guarantees this
            raise LLMResponseError("The reply claimed success without a test.")

        test, warnings = _build_test(payload.test, context, framework)

        result = TestGenerationResult(
            outcome=TestGenerationOutcome.GENERATED,
            test=test,
            prompt_version=prompt.PROMPT_ID,
            llm_provider=completion.provider,
            llm_model=completion.model,
            warnings=warnings,
            generated_at=datetime.now(tz=UTC),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        _log_result(result, issue_number)
        return result

    def _refuse(
        self,
        outcome: TestGenerationOutcome,
        detail: InsufficientContext,
        *,
        started: float,
        issue_number: int,
    ) -> TestGenerationResult:
        """Return a pre-flight refusal without contacting a provider."""
        result = TestGenerationResult(
            outcome=outcome,
            insufficient_context=detail,
            prompt_version=prompt.PROMPT_ID,
            generated_at=datetime.now(tz=UTC),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "Refused to generate a reproduction test",
            extra={
                "issue_number": issue_number,
                "outcome": outcome.value,
                "reason": detail.reason,
                "called_provider": False,
            },
        )
        return result

    def _render_context(self, context: VerificationContext) -> str:
        """Order and truncate the context files shown to the model."""
        snippets = {snippet.path: snippet for snippet in context.snippets}
        reasons = {file.path: file.reasons for file in context.relevant_files}

        # Retrieval order first, then any snippet whose path was not ranked.
        ordered = [file.path for file in context.relevant_files]
        ordered += [path for path in snippets if path not in reasons]
        ordered = ordered[: max(1, self._settings.test_generation_max_context_files)]

        files: list[tuple[str, list[str], str | None]] = []
        for path in ordered:
            snippet = snippets.get(path)
            files.append(
                (
                    path,
                    list(reasons.get(path, ())),
                    snippet.content if snippet else None,
                )
            )

        return prompt.render_context_files(
            files,
            snippet_char_limit=self._settings.test_generation_snippet_char_limit,
            total_char_limit=self._settings.test_generation_context_char_limit,
        )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(text: str, issue_number: int) -> TestGenerationPayload:
    """Parse and validate a reply, refusing anything that does not fit."""
    payload = extract_json_object(text)

    try:
        return TestGenerationPayload.model_validate(payload)
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        logger.warning(
            "Test generation reply failed validation",
            extra={"issue_number": issue_number, "problems": problems},
        )
        raise LLMResponseError(
            f"The test generation reply did not match the expected schema: {problems}"
        ) from exc


def _build_test(
    payload: GeneratedTestPayload,
    context: VerificationContext,
    framework: TestFramework,
) -> tuple[GeneratedReproductionTest, tuple[str, ...]]:
    """Check a generated test against its context and finalise it."""
    warnings: list[str] = []
    available = context.available_paths

    if payload.framework is not framework:
        raise LLMResponseError(
            f"The generated test targets {payload.framework.value}, but the "
            f"repository uses {framework.value}."
        )

    _validate_filename(payload.filename, payload.language)

    # The load-bearing check: an import the context does not contain is a file
    # the model invented, and the test could not even be loaded.
    unresolved = sorted(
        _unresolved_imports(payload.source, payload.filename, available)
    )
    if unresolved:
        raise LLMResponseError(
            "The generated test imports files that are not in the retrieved "
            f"context: {', '.join(unresolved)}."
        )

    referenced = tuple(path for path in payload.referenced_files if path in available)
    if len(referenced) != len(payload.referenced_files):
        dropped = sorted(set(payload.referenced_files) - available)
        warnings.append(
            "Dropped referenced_files entries outside the retrieved context: "
            f"{', '.join(dropped)}."
        )

    declared = _declared_dependencies(context)
    dependencies = tuple(
        RequiredDependency(
            name=dependency.name,
            version_spec=dependency.version_spec,
            reason=dependency.reason,
            already_present=dependency.name in declared,
        )
        for dependency in payload.required_dependencies
    )

    test = GeneratedReproductionTest(
        language=payload.language,
        framework=payload.framework,
        filename=payload.filename,
        source=payload.source,
        assumptions=payload.assumptions,
        reasoning_summary=payload.reasoning_summary,
        confidence=payload.confidence,
        expected_outcome=payload.expected_outcome,
        required_dependencies=dependencies,
        referenced_files=referenced,
    )
    return test, tuple(warnings)


def _validate_filename(filename: str, language: TestLanguage) -> None:
    """Reject a path that is unsafe, misnamed, or would not be collected."""
    if filename.startswith("/") or ".." in PurePosixPath(filename).parts:
        raise LLMResponseError(
            f"The generated test filename {filename!r} is not a safe "
            "repository-relative path."
        )

    suffix = PurePosixPath(filename).suffix
    if suffix not in _LANGUAGE_EXTENSIONS[language]:
        raise LLMResponseError(
            f"The generated test filename {filename!r} does not match its "
            f"declared language {language.value}."
        )

    if not any(marker in filename for marker in _DISCOVERY_MARKERS):
        raise LLMResponseError(
            f"The generated test filename {filename!r} contains neither "
            "'.test.' nor '.spec.', so the framework would not collect it."
        )


def _unresolved_imports(
    source: str, filename: str, available: frozenset[str]
) -> set[str]:
    """Relative module specifiers that do not resolve inside the context."""
    unresolved: set[str] = set()

    for specifier in _MODULE_REFERENCE_RE.findall(source):
        base = _normalize(f"{PurePosixPath(filename).parent}/{specifier}")
        if base is None:
            unresolved.add(specifier)
            continue
        if not any(candidate in available for candidate in _candidates(base)):
            unresolved.add(specifier)

    return unresolved


def _candidates(base: str) -> Iterator[str]:
    """Module resolution attempts, in Node's order."""
    yield base

    suffix = PurePosixPath(base).suffix
    for source_extension in _EMITTED_TO_SOURCE.get(suffix, ()):
        yield f"{base[: -len(suffix)]}{source_extension}"

    for extension in _MODULE_EXTENSIONS:
        yield f"{base}{extension}"
    for extension in _MODULE_EXTENSIONS:
        yield f"{base}/index{extension}"


def _normalize(path: str) -> str | None:
    """Collapse `.` and `..`, refusing anything that escapes the root."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _declared_dependencies(context: VerificationContext) -> frozenset[str]:
    """Packages the repository manifest already declares."""
    root_package = context.repository_analysis.root_package
    return root_package.dependencies if root_package is not None else frozenset()


# --------------------------------------------------------------------------
# Prompt blocks
# --------------------------------------------------------------------------


def _repository_block(context: VerificationContext) -> dict[str, object]:
    repository = context.repository
    return {
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "language": repository.language,
        "description": repository.description,
    }


def _analysis_block(context: VerificationContext) -> dict[str, object]:
    analysis = context.repository_analysis
    return {
        "primary_language": (
            analysis.primary_language.value if analysis.primary_language else None
        ),
        "uses_typescript": analysis.uses_typescript,
        "test_framework": (
            analysis.test_framework.value if analysis.test_framework else None
        ),
        "test_command": analysis.test_command,
        "package_manager": (
            analysis.package_manager.value if analysis.package_manager else None
        ),
        "is_monorepo": analysis.is_monorepo,
        "source_directories": list(analysis.source_directories),
        "test_directories": list(analysis.test_directories),
        "declared_dependencies": sorted(_declared_dependencies(context)),
    }


def _issue_block(context: VerificationContext) -> dict[str, object]:
    analysis = context.issue_analysis
    return {
        "issue_number": analysis.issue_number,
        "summary": analysis.summary,
        "expected_behavior": (
            analysis.expected_behavior.model_dump(mode="json")
            if analysis.expected_behavior
            else None
        ),
        "observed_behavior": (
            analysis.observed_behavior.model_dump(mode="json")
            if analysis.observed_behavior
            else None
        ),
        "reproduction_steps": [
            step.model_dump(mode="json") for step in analysis.reproduction_steps
        ],
        "environment": [
            detail.model_dump(mode="json") for detail in analysis.environment
        ],
        "mentioned_entities": [
            entity.model_dump(mode="json") for entity in analysis.mentioned_entities
        ],
        "prerequisites": [
            item.model_dump(mode="json") for item in analysis.prerequisites
        ],
        "ambiguities": list(analysis.ambiguities),
        "missing_information": [
            item.model_dump(mode="json") for item in analysis.missing_information
        ],
        "reproducibility": analysis.reproducibility.model_dump(mode="json"),
    }


def _log_result(result: TestGenerationResult, issue_number: int) -> None:
    test = result.test
    logger.info(
        "Generated reproduction test" if result.succeeded else "Declined to generate",
        extra={
            "issue_number": issue_number,
            "outcome": result.outcome.value,
            "prompt_version": result.prompt_version,
            "llm_model": result.llm_model,
            "filename": test.filename if test else None,
            "framework": test.framework.value if test else None,
            "confidence": (
                test.confidence.value
                if test
                else (
                    result.insufficient_context.confidence.value
                    if result.insufficient_context
                    else Confidence.LOW.value
                )
            ),
            "source_chars": len(test.source) if test else 0,
            "dependency_count": len(test.required_dependencies) if test else 0,
            "warning_count": len(result.warnings),
            "duration_ms": result.duration_ms,
        },
    )
