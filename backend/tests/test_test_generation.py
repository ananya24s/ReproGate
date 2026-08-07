"""Unit tests for AI-assisted reproduction test generation.

Every test drives a fake :class:`LLMProvider`, so the suite needs no API key
and makes no network call. What is under test is the deterministic half:
pre-flight refusals, parsing, schema validation, and the guarantees ReproGate
enforces on whatever the model returns — above all that a generated test never
imports a file the context did not supply.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import LLMError, LLMResponseError
from app.llm.prompts import test_generation as prompt
from app.llm.provider import LLMProvider
from app.llm.schemas import LLMCompletion, LLMCompletionRequest, LLMRole
from app.repository_analysis.models import (
    Language,
    LanguageUsage,
    NodePackage,
    PackageManager,
    RepositoryAnalysis,
    TestFramework,
)
from app.schemas.github import GitHubRepository
from app.schemas.issue_analysis import (
    Confidence,
    EvidenceBasis,
    ExtractedStatement,
    IssueAnalysis,
    ReproducibilityAssessment,
    ReproductionStep,
)
from app.schemas.test_generation import (
    ExpectedSignal,
    FileSnippet,
    RelevantFile,
    TestGenerationOutcome,
    TestLanguage,
    VerificationContext,
)
from app.verification.test_generator import ReproductionTestGenerator


class FakeLLMProvider(LLMProvider):
    """Returns a canned reply, or raises, and records what it was asked."""

    def __init__(self, reply: str = "", *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.requests: list[LLMCompletionRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model-1"

    async def complete(self, request: LLMCompletionRequest) -> LLMCompletion:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return LLMCompletion(
            text=self._reply, provider=self.name, model=self.model, duration_ms=1
        )


@pytest.fixture
def settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------
# Context fixtures
# --------------------------------------------------------------------------


def make_repository() -> GitHubRepository:
    return GitHubRepository(
        owner="octocat",
        name="widget",
        full_name="octocat/widget",
        default_branch="main",
        clone_url="https://github.com/octocat/widget.git",
        html_url="https://github.com/octocat/widget",
        language="TypeScript",
    )


def make_repository_analysis(
    framework: TestFramework | None = TestFramework.VITEST,
    dependencies: frozenset[str] = frozenset({"vitest", "zod"}),
) -> RepositoryAnalysis:
    return RepositoryAnalysis(
        root="/tmp/widget",
        languages=(LanguageUsage(language=Language.TYPESCRIPT, file_count=10),),
        primary_language=Language.TYPESCRIPT,
        uses_typescript=True,
        is_node_project=True,
        root_package=NodePackage(
            path="package.json",
            directory=".",
            name="widget",
            scripts={"test": "vitest run"},
            dependencies=dependencies,
        ),
        package_manager=PackageManager.PNPM,
        test_framework=framework,
        test_command="pnpm run test",
        source_directories=("src",),
        test_directories=("tests",),
        analyzed_at=datetime.now(tz=UTC),
    )


def make_issue_analysis(*, with_steps: bool = True) -> IssueAnalysis:
    return IssueAnalysis(
        issue_number=42,
        summary="The reporter observes that parseConfig throws on an empty file.",
        expected_behavior=ExtractedStatement(
            text="parseConfig returns an empty object.",
            basis=EvidenceBasis.STATED,
            confidence=Confidence.HIGH,
        ),
        observed_behavior=ExtractedStatement(
            text="parseConfig raises a TypeError.",
            basis=EvidenceBasis.STATED,
            confidence=Confidence.HIGH,
        ),
        reproduction_steps=(
            (
                ReproductionStep(
                    order=1,
                    action="Call parseConfig with an empty file",
                    basis=EvidenceBasis.STATED,
                ),
            )
            if with_steps
            else ()
        ),
        reproducibility=ReproducibilityAssessment(
            sufficient_for_reproduction=with_steps,
            confidence=Confidence.HIGH if with_steps else Confidence.LOW,
            rationale="The report describes the call and the symptom.",
        ),
        prompt_version="issue_analysis/v1",
        llm_provider="fake",
        llm_model="fake-model-1",
        analyzed_at=datetime.now(tz=UTC),
    )


def make_context(
    *,
    framework: TestFramework | None = TestFramework.VITEST,
    files: tuple[str, ...] = ("src/config/parser.ts",),
    with_snippets: bool = True,
    dependencies: frozenset[str] = frozenset({"vitest", "zod"}),
) -> VerificationContext:
    return VerificationContext(
        issue_analysis=make_issue_analysis(),
        repository_analysis=make_repository_analysis(framework, dependencies),
        repository=make_repository(),
        relevant_files=tuple(
            RelevantFile(path=path, score=10.0, reasons=("named in the issue",))
            for path in files
        ),
        snippets=(
            tuple(
                FileSnippet(path=path, content=f"// contents of {path}\n")
                for path in files
            )
            if with_snippets
            else ()
        ),
    )


# --------------------------------------------------------------------------
# Reply fixtures
# --------------------------------------------------------------------------


SOURCE: str = (
    "import { describe, it, expect } from 'vitest';\n"
    "import { parseConfig } from '../src/config/parser';\n"
    "\n"
    "describe('parseConfig', () => {\n"
    "  it('does not throw on an empty file', () => {\n"
    "    expect(() => parseConfig('')).not.toThrow();\n"
    "  });\n"
    "});\n"
)


SOURCE_WITHOUT_IMPORTS: str = (
    "import { describe, it, expect } from 'vitest';\n"
    "describe('placeholder', () => { it('runs', () => { expect(1).toBe(1); }); });\n"
)


def generated_test_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "language": "typescript",
        "framework": "vitest",
        "filename": "tests/parse-config.repro.test.ts",
        "source": SOURCE,
        "assumptions": ["parseConfig is exported from src/config/parser.ts"],
        "reasoning_summary": "Calls parseConfig with the reported input.",
        "confidence": "medium",
        "expected_outcome": {
            "signal": "fails_when_issue_present",
            "description": "The assertion would fail if parseConfig throws.",
        },
        "required_dependencies": [
            {"name": "vitest", "version_spec": "^2.0.0", "reason": "test runner"},
            {"name": "left-pad", "version_spec": None, "reason": "helper"},
        ],
        "referenced_files": ["src/config/parser.ts"],
    }
    base.update(overrides)
    return base


def reply(**overrides: Any) -> dict[str, Any]:
    return {"outcome": "generated", "test": generated_test_payload(**overrides)}


def refusal_reply() -> dict[str, Any]:
    return {
        "outcome": "insufficient_context",
        "insufficient_context": {
            "reason": "The parser source was not supplied.",
            "missing": ["the body of parseConfig"],
            "confidence": "high",
        },
    }


def generator_for(
    body: dict[str, Any] | str, settings: Settings
) -> tuple[ReproductionTestGenerator, FakeLLMProvider]:
    text = body if isinstance(body, str) else json.dumps(body)
    provider = FakeLLMProvider(text)
    return ReproductionTestGenerator(provider, settings), provider


# --------------------------------------------------------------------------
# Successful generation
# --------------------------------------------------------------------------


async def test_successful_generation(settings: Settings) -> None:
    generator, _ = generator_for(reply(), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED
    assert result.succeeded is True
    assert result.insufficient_context is None

    test = result.test
    assert test is not None
    assert test.language is TestLanguage.TYPESCRIPT
    assert test.framework is TestFramework.VITEST
    assert test.filename == "tests/parse-config.repro.test.ts"
    assert "parseConfig" in test.source
    assert test.assumptions
    assert test.reasoning_summary
    assert test.confidence is Confidence.MEDIUM
    assert test.expected_outcome.signal is ExpectedSignal.FAILS_WHEN_ISSUE_PRESENT
    assert test.referenced_files == ("src/config/parser.ts",)
    assert result.warnings == ()


async def test_dependency_presence_is_resolved_not_trusted(
    settings: Settings,
) -> None:
    generator, _ = generator_for(reply(), settings)

    result = await generator.generate(make_context())

    assert result.test is not None
    presence = {
        dependency.name: dependency.already_present
        for dependency in result.test.required_dependencies
    }
    # `vitest` is declared in the manifest; `left-pad` is not.
    assert presence == {"vitest": True, "left-pad": False}


async def test_provenance_is_recorded(settings: Settings) -> None:
    generator, _ = generator_for(reply(), settings)

    result = await generator.generate(make_context())

    assert result.prompt_version == prompt.PROMPT_ID == "test_generation/v1"
    assert result.llm_provider == "fake"
    assert result.llm_model == "fake-model-1"
    assert result.generated_at.tzinfo is not None
    assert result.duration_ms >= 0


# --------------------------------------------------------------------------
# Insufficient context
# --------------------------------------------------------------------------


async def test_no_context_files_refuses_without_calling_the_provider(
    settings: Settings,
) -> None:
    generator, provider = generator_for(reply(), settings)

    result = await generator.generate(make_context(files=(), with_snippets=False))

    assert result.outcome is TestGenerationOutcome.INSUFFICIENT_CONTEXT
    assert result.test is None
    assert result.insufficient_context is not None
    assert result.insufficient_context.missing
    assert provider.requests == []
    assert result.llm_model is None


async def test_model_may_declare_insufficient_context(settings: Settings) -> None:
    generator, provider = generator_for(refusal_reply(), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.INSUFFICIENT_CONTEXT
    assert result.test is None
    assert result.insufficient_context is not None
    assert "parser source" in result.insufficient_context.reason
    assert result.insufficient_context.confidence is Confidence.HIGH
    assert provider.requests, "the provider should have been consulted"


# --------------------------------------------------------------------------
# Unsupported framework
# --------------------------------------------------------------------------


async def test_missing_framework_refuses_without_calling_the_provider(
    settings: Settings,
) -> None:
    generator, provider = generator_for(reply(), settings)

    result = await generator.generate(make_context(framework=None))

    assert result.outcome is TestGenerationOutcome.UNSUPPORTED_FRAMEWORK
    assert result.test is None
    assert result.insufficient_context is not None
    assert provider.requests == []


async def test_generated_framework_must_match_the_repository(
    settings: Settings,
) -> None:
    generator, _ = generator_for(reply(framework="jest"), settings)

    with pytest.raises(LLMResponseError, match="jest"):
        await generator.generate(make_context(framework=TestFramework.VITEST))


async def test_jest_repository_generates_a_jest_test(settings: Settings) -> None:
    source = SOURCE.replace("vitest", "@jest/globals")
    generator, _ = generator_for(reply(framework="jest", source=source), settings)

    result = await generator.generate(make_context(framework=TestFramework.JEST))

    assert result.test is not None
    assert result.test.framework is TestFramework.JEST


# --------------------------------------------------------------------------
# Never invent repository files
# --------------------------------------------------------------------------


async def test_import_outside_the_context_is_rejected(settings: Settings) -> None:
    source = SOURCE.replace("'../src/config/parser'", "'../src/config/does-not-exist'")
    generator, _ = generator_for(reply(source=source), settings)

    with pytest.raises(LLMResponseError, match="not in the retrieved context"):
        await generator.generate(make_context())


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("import x from '../src/ghost';", id="default-import"),
        pytest.param("const x = require('../src/ghost');", id="require"),
        pytest.param("vi.mock('../src/ghost');", id="vitest-mock"),
        pytest.param("jest.mock('../src/ghost');", id="jest-mock"),
        pytest.param("const x = await import('../src/ghost');", id="dynamic-import"),
    ],
)
async def test_invented_files_are_caught_in_every_module_position(
    settings: Settings, statement: str
) -> None:
    generator, _ = generator_for(reply(source=f"{SOURCE}\n{statement}\n"), settings)

    with pytest.raises(LLMResponseError, match="not in the retrieved context"):
        await generator.generate(make_context())


async def test_runtime_paths_are_not_mistaken_for_imports(
    settings: Settings,
) -> None:
    # A fixture the test writes and reads itself is not a module reference.
    source = SOURCE + "\nwriteFileSync('./tmp/fixture.json', '{}');\n"
    generator, _ = generator_for(reply(source=source), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED


async def test_bare_package_imports_are_allowed(settings: Settings) -> None:
    source = "import { z } from 'zod';\n" + SOURCE
    generator, _ = generator_for(reply(source=source), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED


async def test_imports_resolving_to_a_directory_index_are_allowed(
    settings: Settings,
) -> None:
    source = SOURCE.replace("'../src/config/parser'", "'../src/config'")
    generator, _ = generator_for(reply(source=source), settings)

    result = await generator.generate(make_context(files=("src/config/index.ts",)))

    assert result.outcome is TestGenerationOutcome.GENERATED


async def test_typescript_esm_js_extension_resolves_to_the_source(
    settings: Settings,
) -> None:
    source = SOURCE.replace("'../src/config/parser'", "'../src/config/parser.js'")
    generator, _ = generator_for(reply(source=source), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED


async def test_referenced_files_outside_the_context_are_dropped(
    settings: Settings,
) -> None:
    generator, _ = generator_for(
        reply(referenced_files=["src/config/parser.ts", "src/ghost.ts"]), settings
    )

    result = await generator.generate(make_context())

    assert result.test is not None
    assert result.test.referenced_files == ("src/config/parser.ts",)
    assert len(result.warnings) == 1
    assert "src/ghost.ts" in result.warnings[0]


# --------------------------------------------------------------------------
# Filename safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        pytest.param("/etc/passwd.test.ts", "safe", id="absolute"),
        pytest.param("../outside.test.ts", "safe", id="parent-traversal"),
        pytest.param("tests/repro.ts", "collect", id="no-discovery-marker"),
        pytest.param("tests/repro.test.py", "language", id="wrong-extension"),
        pytest.param("tests/repro.test.js", "language", id="js-for-typescript"),
    ],
)
async def test_unsafe_or_uncollectable_filenames_are_rejected(
    settings: Settings, filename: str, expected: str
) -> None:
    generator, _ = generator_for(reply(filename=filename), settings)

    with pytest.raises(LLMResponseError, match=expected):
        await generator.generate(make_context())


async def test_spec_naming_is_accepted(settings: Settings) -> None:
    generator, _ = generator_for(reply(filename="tests/parse-config.spec.ts"), settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED


# --------------------------------------------------------------------------
# Malformed output and invalid schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("I cannot write this test.", id="prose-only"),
        pytest.param("{ not valid json", id="broken-json"),
        pytest.param("[1, 2, 3]", id="json-array"),
    ],
)
async def test_malformed_replies_raise_a_domain_error(
    settings: Settings, body: str
) -> None:
    generator, _ = generator_for(body, settings)

    with pytest.raises(LLMResponseError):
        await generator.generate(make_context())


@pytest.mark.parametrize(
    ("body", "expected_in_message"),
    [
        pytest.param({"outcome": "generated"}, "test", id="generated-without-test"),
        pytest.param(
            {"outcome": "insufficient_context"},
            "insufficient_context",
            id="refusal-without-detail",
        ),
        pytest.param({"outcome": "maybe"}, "outcome", id="unknown-outcome"),
        pytest.param(
            {"outcome": "generated", "test": generated_test_payload(language="cobol")},
            "language",
            id="unknown-language",
        ),
        pytest.param(
            {"outcome": "generated", "test": generated_test_payload(framework="mocha")},
            "framework",
            id="unsupported-framework-value",
        ),
        pytest.param(
            {"outcome": "generated", "test": generated_test_payload(source="")},
            "source",
            id="empty-source",
        ),
        pytest.param(
            {
                "outcome": "generated",
                "test": generated_test_payload(expected_outcome={"signal": "it works"}),
            },
            "expected_outcome",
            id="bad-expected-outcome",
        ),
        pytest.param(
            {"outcome": "generated", "test": generated_test_payload(), "extra": 1},
            "extra",
            id="unknown-top-level-key",
        ),
    ],
)
async def test_schema_violations_raise_a_diagnosable_error(
    settings: Settings, body: dict[str, Any], expected_in_message: str
) -> None:
    generator, _ = generator_for(body, settings)

    with pytest.raises(LLMResponseError) as exc_info:
        await generator.generate(make_context())

    assert expected_in_message in str(exc_info.value)
    assert exc_info.value.error_code == "llm_response_error"


async def test_reply_in_a_code_fence_is_accepted(settings: Settings) -> None:
    generator, _ = generator_for(f"```json\n{json.dumps(reply())}\n```", settings)

    result = await generator.generate(make_context())

    assert result.outcome is TestGenerationOutcome.GENERATED


# --------------------------------------------------------------------------
# Provider failure
# --------------------------------------------------------------------------


async def test_provider_failure_propagates(settings: Settings) -> None:
    provider = FakeLLMProvider(error=LLMError("upstream is unavailable"))
    generator = ReproductionTestGenerator(provider, settings)

    with pytest.raises(LLMError, match="upstream is unavailable"):
        await generator.generate(make_context())


async def test_provider_timeout_is_not_swallowed(settings: Settings) -> None:
    provider = FakeLLMProvider(error=TimeoutError("timed out"))
    generator = ReproductionTestGenerator(provider, settings)

    with pytest.raises(TimeoutError):
        await generator.generate(make_context())


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


async def test_prompt_states_the_constraints_and_the_context(
    settings: Settings,
) -> None:
    generator, provider = generator_for(reply(), settings)

    await generator.generate(make_context())

    system, user = provider.requests[0].messages
    assert system.role is LLMRole.SYSTEM
    assert user.role is LLMRole.USER

    assert "USE ONLY THE SUPPLIED CONTEXT" in system.content
    assert "NEVER INVENT A FILE" in system.content
    assert "NEVER CLAIM THE ISSUE REPRODUCES" in system.content

    assert "octocat/widget" in user.content
    assert "vitest" in user.content
    assert "src/config/parser.ts" in user.content
    assert "parseConfig throws" in user.content
    assert "ONLY files that exist" in user.content


async def test_only_context_files_reach_the_prompt(settings: Settings) -> None:
    generator, provider = generator_for(reply(), settings)

    await generator.generate(make_context(files=("src/config/parser.ts",)))

    user = provider.requests[0].messages[1].content
    assert "src/config/parser.ts" in user
    assert "src/unrelated/logger.ts" not in user


async def test_context_files_are_capped(settings: Settings) -> None:
    generator, provider = generator_for(
        reply(source=SOURCE_WITHOUT_IMPORTS),
        settings.model_copy(update={"test_generation_max_context_files": 2}),
    )

    await generator.generate(
        make_context(files=("src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"))
    )

    user = provider.requests[0].messages[1].content
    assert "src/a.ts" in user and "src/b.ts" in user
    assert "src/c.ts" not in user and "src/d.ts" not in user


async def test_long_snippets_are_truncated_visibly(settings: Settings) -> None:
    context = VerificationContext(
        issue_analysis=make_issue_analysis(),
        repository_analysis=make_repository_analysis(),
        repository=make_repository(),
        relevant_files=(RelevantFile(path="src/config/parser.ts"),),
        snippets=(FileSnippet(path="src/config/parser.ts", content="x" * 5000),),
    )
    generator, provider = generator_for(
        reply(), settings.model_copy(update={"test_generation_snippet_char_limit": 100})
    )

    await generator.generate(context)

    user = provider.requests[0].messages[1].content
    assert "[snippet truncated by ReproGate]" in user
    assert "x" * 200 not in user


async def test_request_asks_for_deterministic_json(settings: Settings) -> None:
    generator, provider = generator_for(reply(), settings)

    await generator.generate(make_context())

    request = provider.requests[0]
    assert request.expects_json is True
    assert request.temperature == 0.0
    assert request.response_schema is not None


def test_response_schema_is_derived_from_the_validating_model() -> None:
    from app.schemas.test_generation import TestGenerationPayload
    from app.verification.test_generator import RESPONSE_SCHEMA

    assert TestGenerationPayload.model_json_schema() == RESPONSE_SCHEMA


async def test_generation_is_deterministic_for_a_fixed_reply(
    settings: Settings,
) -> None:
    generator, _ = generator_for(reply(), settings)
    context = make_context()

    first = await generator.generate(context)
    second = await generator.generate(context)

    ignore = {"generated_at", "duration_ms"}
    assert first.model_dump(exclude=ignore) == second.model_dump(exclude=ignore)
