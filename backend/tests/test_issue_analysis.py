"""Unit tests for AI-assisted issue analysis.

Every test drives a fake :class:`LLMProvider`, so the suite never needs an API
key and never makes a network call. What is under test is the deterministic
half: prompt construction, parsing, validation, and the guarantees ReproGate
enforces on top of whatever the model returns.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import LLMError, LLMResponseError
from app.llm.prompts import issue_analysis as prompt
from app.llm.provider import LLMProvider
from app.llm.schemas import (
    LLMCompletion,
    LLMCompletionRequest,
    LLMRole,
    extract_json_object,
)
from app.schemas.github import GitHubIssue, GitHubIssueState
from app.schemas.issue_analysis import (
    Confidence,
    EvidenceBasis,
    IssueField,
    MentionKind,
)
from app.verification.issue_analyzer import IssueAnalyzer


class FakeLLMProvider(LLMProvider):
    """Returns a canned reply, or raises, and records what it was asked."""

    def __init__(self, reply: str = "", *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.requests: list[LLMCompletionRequest] = []
        self.closed = False

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
            text=self._reply,
            provider=self.name,
            model=self.model,
            duration_ms=1,
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings() -> Settings:
    return Settings()


def make_issue(
    title: str = "Crash when parsing an empty config",
    body: str | None = "It throws.",
    *,
    number: int = 42,
    labels: tuple[str, ...] = ("bug",),
    state: GitHubIssueState = GitHubIssueState.OPEN,
) -> GitHubIssue:
    return GitHubIssue(
        number=number,
        title=title,
        body=body,
        state=state,
        author="reporter",
        labels=labels,
        html_url="https://github.com/octocat/hello-world/issues/42",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def payload(**overrides: Any) -> dict[str, Any]:
    """A minimally valid analysis payload, overridable per test."""
    base: dict[str, Any] = {
        "summary": "The reporter observes a crash when the config file is empty.",
        "expected_behavior": None,
        "observed_behavior": None,
        "reproduction_steps": [],
        "environment": [],
        "mentioned_entities": [],
        "prerequisites": [],
        "configuration_indicators": [],
        "stale_or_fixed_indicators": [],
        "ambiguities": [],
        "missing_information": [],
        "reproducibility": {
            "sufficient_for_reproduction": False,
            "confidence": "low",
            "rationale": "The report does not describe how to reproduce it.",
            "blocking_gaps": ["no reproduction steps"],
        },
    }
    base.update(overrides)
    return base


def analyzer_for(
    reply: dict[str, Any] | str, settings: Settings
) -> tuple[IssueAnalyzer, FakeLLMProvider]:
    text = reply if isinstance(reply, str) else json.dumps(reply)
    provider = FakeLLMProvider(text)
    return IssueAnalyzer(provider, settings), provider


# --------------------------------------------------------------------------
# Complete issue
# --------------------------------------------------------------------------


COMPLETE_PAYLOAD: dict[str, Any] = payload(
    summary="The reporter observes that parseConfig throws on an empty file.",
    expected_behavior={
        "text": "parseConfig returns an empty object for an empty file.",
        "basis": "stated",
        "confidence": "high",
        "source_quote": "I expected an empty object",
    },
    observed_behavior={
        "text": "parseConfig raises a TypeError.",
        "basis": "stated",
        "confidence": "high",
        "source_quote": "it throws TypeError",
    },
    reproduction_steps=[
        {
            "order": 1,
            "action": "Create an empty config.json",
            "basis": "stated",
            "source_quote": "create an empty config.json",
        },
        {
            "order": 2,
            "action": "Call parseConfig('config.json')",
            "basis": "stated",
            "source_quote": "call parseConfig",
        },
    ],
    environment=[
        {"name": "node", "value": "20.11.0", "basis": "stated"},
        {"name": "os", "value": "macOS 14", "basis": "stated"},
    ],
    mentioned_entities=[
        {"kind": "file", "value": "src/config/parser.ts", "basis": "stated"},
        {"kind": "function", "value": "parseConfig", "basis": "stated"},
        {"kind": "package", "value": "zod", "basis": "stated"},
        {"kind": "command", "value": "npm run build", "basis": "stated"},
        {"kind": "error_message", "value": "TypeError: cannot read", "basis": "stated"},
    ],
    prerequisites=[
        {
            "text": "The project must be built first.",
            "basis": "stated",
            "confidence": "medium",
            "source_quote": "after running npm run build",
        }
    ],
    reproducibility={
        "sufficient_for_reproduction": True,
        "confidence": "high",
        "rationale": "The report gives ordered steps and an expected result.",
        "blocking_gaps": [],
    },
)


async def test_complete_issue_is_fully_extracted(settings: Settings) -> None:
    analyzer, _ = analyzer_for(COMPLETE_PAYLOAD, settings)

    analysis = await analyzer.analyze(make_issue(), repository="octocat/hello-world")

    assert analysis.issue_number == 42
    assert analysis.summary.startswith("The reporter observes")
    assert analysis.expected_behavior is not None
    assert analysis.expected_behavior.basis is EvidenceBasis.STATED
    assert analysis.observed_behavior is not None
    assert analysis.observed_behavior.source_quote == "it throws TypeError"

    assert [step.order for step in analysis.reproduction_steps] == [1, 2]
    assert analysis.has_reproduction_steps is True

    assert {detail.name for detail in analysis.environment} == {"node", "os"}
    assert {entity.kind for entity in analysis.mentioned_entities} == {
        MentionKind.FILE,
        MentionKind.FUNCTION,
        MentionKind.PACKAGE,
        MentionKind.COMMAND,
        MentionKind.ERROR_MESSAGE,
    }
    assert len(analysis.prerequisites) == 1
    assert analysis.reproducibility.sufficient_for_reproduction is True
    assert analysis.warnings == ()


# --------------------------------------------------------------------------
# Sparse, ambiguous, and stepless issues
# --------------------------------------------------------------------------


async def test_sparse_issue_records_missing_information(settings: Settings) -> None:
    analyzer, _ = analyzer_for(
        payload(
            missing_information=[
                {"field": "reproduction_steps", "note": "No steps are given."},
                {"field": "expected_behavior", "note": "The report never says."},
                {"field": "version", "note": "No version is stated."},
            ]
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue("It broke", body=None))

    assert analysis.expected_behavior is None
    assert analysis.observed_behavior is None
    assert analysis.reproduction_steps == ()
    assert {entry.field for entry in analysis.missing_information} == {
        IssueField.REPRODUCTION_STEPS,
        IssueField.EXPECTED_BEHAVIOR,
        IssueField.VERSION,
    }
    assert analysis.reproducibility.sufficient_for_reproduction is False


async def test_missing_reproduction_steps_are_not_invented(settings: Settings) -> None:
    analyzer, _ = analyzer_for(
        payload(
            missing_information=[
                {"field": "reproduction_steps", "note": "The issue gives none."}
            ]
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue(body="Something is wrong."))

    assert analysis.reproduction_steps == ()
    assert analysis.has_reproduction_steps is False
    assert analysis.reproducibility.blocking_gaps


async def test_ambiguous_issue_preserves_uncertainty(settings: Settings) -> None:
    analyzer, _ = analyzer_for(
        payload(
            expected_behavior={
                "text": "The reporter may expect the call to succeed.",
                "basis": "inferred",
                "confidence": "low",
                "source_quote": None,
            },
            ambiguities=[
                "It is unclear which version is affected.",
                "The report does not say whether this happens on every run.",
            ],
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue("Sometimes fails"))

    assert analysis.expected_behavior is not None
    assert analysis.expected_behavior.basis is EvidenceBasis.INFERRED
    assert analysis.expected_behavior.confidence is Confidence.LOW
    assert analysis.expected_behavior.source_quote is None
    assert len(analysis.ambiguities) == 2


async def test_configuration_and_staleness_indicators_are_kept(
    settings: Settings,
) -> None:
    analyzer, _ = analyzer_for(
        payload(
            configuration_indicators=[
                {
                    "signal": "custom tsconfig paths",
                    "rationale": "The reporter shows a non-default tsconfig.",
                    "confidence": "medium",
                }
            ],
            stale_or_fixed_indicators=[
                {
                    "signal": "old version",
                    "rationale": "The reporter is on 1.0.0 while 3.x is current.",
                    "confidence": "high",
                }
            ],
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue())

    assert analysis.configuration_indicators[0].signal == "custom tsconfig paths"
    assert analysis.stale_or_fixed_indicators[0].confidence is Confidence.HIGH


# --------------------------------------------------------------------------
# Never invent reproduction steps
# --------------------------------------------------------------------------


async def test_inferred_reproduction_steps_are_discarded(settings: Settings) -> None:
    analyzer, _ = analyzer_for(
        payload(
            reproduction_steps=[
                {"order": 1, "action": "Install the package", "basis": "stated"},
                {"order": 2, "action": "Probably run the build", "basis": "inferred"},
                {"order": 3, "action": "Call parseConfig", "basis": "stated"},
            ]
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue())

    assert [step.action for step in analysis.reproduction_steps] == [
        "Install the package",
        "Call parseConfig",
    ]
    # Survivors are renumbered so the sequence stays contiguous.
    assert [step.order for step in analysis.reproduction_steps] == [1, 2]
    assert all(
        step.basis is EvidenceBasis.STATED for step in analysis.reproduction_steps
    )
    assert len(analysis.warnings) == 1
    assert "Discarded 1 reproduction step" in analysis.warnings[0]


async def test_entirely_inferred_steps_leave_none(settings: Settings) -> None:
    analyzer, _ = analyzer_for(
        payload(
            reproduction_steps=[
                {"order": 1, "action": "Presumably install it", "basis": "inferred"},
            ]
        ),
        settings,
    )

    analysis = await analyzer.analyze(make_issue())

    assert analysis.reproduction_steps == ()
    assert analysis.warnings


# --------------------------------------------------------------------------
# Malformed and invalid model output
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n  ", id="whitespace"),
        pytest.param("I cannot analyse this issue.", id="prose-only"),
        pytest.param("{ not valid json", id="broken-json"),
        pytest.param("[1, 2, 3]", id="json-array"),
        pytest.param('"a string"', id="json-scalar"),
    ],
)
async def test_unparseable_replies_raise_a_domain_error(
    settings: Settings, reply: str
) -> None:
    analyzer, _ = analyzer_for(reply, settings)

    with pytest.raises(LLMResponseError):
        await analyzer.analyze(make_issue())


@pytest.mark.parametrize(
    ("mutation", "expected_in_message"),
    [
        pytest.param({"summary": None}, "summary", id="null-required-field"),
        pytest.param(
            {"reproducibility": None}, "reproducibility", id="null-assessment"
        ),
        pytest.param(
            {"reproduction_steps": [{"order": 1, "action": "x", "basis": "guessed"}]},
            "basis",
            id="unknown-enum-value",
        ),
        pytest.param(
            {
                "mentioned_entities": [
                    {"kind": "spaceship", "value": "x", "basis": "stated"}
                ]
            },
            "kind",
            id="unknown-mention-kind",
        ),
        pytest.param(
            {"missing_information": [{"field": "vibes", "note": "x"}]},
            "field",
            id="unknown-issue-field",
        ),
        pytest.param({"unexpected_key": True}, "unexpected_key", id="extra-key"),
    ],
)
async def test_schema_violations_raise_a_diagnosable_error(
    settings: Settings, mutation: dict[str, Any], expected_in_message: str
) -> None:
    analyzer, _ = analyzer_for(payload(**mutation), settings)

    with pytest.raises(LLMResponseError) as exc_info:
        await analyzer.analyze(make_issue())

    # The message names the offending field, so a bad reply is diagnosable.
    assert expected_in_message in str(exc_info.value)
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "llm_response_error"


async def test_reply_wrapped_in_a_code_fence_is_accepted(settings: Settings) -> None:
    fenced = f"```json\n{json.dumps(payload())}\n```"
    analyzer, _ = analyzer_for(fenced, settings)

    analysis = await analyzer.analyze(make_issue())

    assert analysis.summary


async def test_reply_surrounded_by_prose_is_accepted(settings: Settings) -> None:
    wrapped = f"Here is the analysis:\n{json.dumps(payload())}\nHope that helps."
    analyzer, _ = analyzer_for(wrapped, settings)

    analysis = await analyzer.analyze(make_issue())

    assert analysis.summary


# --------------------------------------------------------------------------
# Provider failure
# --------------------------------------------------------------------------


async def test_provider_failure_propagates_as_a_domain_error(
    settings: Settings,
) -> None:
    provider = FakeLLMProvider(error=LLMError("upstream is unavailable"))
    analyzer = IssueAnalyzer(provider, settings)

    with pytest.raises(LLMError) as exc_info:
        await analyzer.analyze(make_issue())

    assert "upstream is unavailable" in str(exc_info.value)


async def test_provider_timeout_is_not_swallowed(settings: Settings) -> None:
    provider = FakeLLMProvider(error=TimeoutError("timed out"))
    analyzer = IssueAnalyzer(provider, settings)

    with pytest.raises(TimeoutError):
        await analyzer.analyze(make_issue())


# --------------------------------------------------------------------------
# Prompt construction and provenance
# --------------------------------------------------------------------------


async def test_prompt_carries_the_issue_and_the_schema(settings: Settings) -> None:
    analyzer, provider = analyzer_for(payload(), settings)

    await analyzer.analyze(
        make_issue("Crash in parseConfig", body="It throws a TypeError."),
        repository="octocat/hello-world",
    )

    request = provider.requests[0]
    system, user = request.messages
    assert system.role is LLMRole.SYSTEM
    assert user.role is LLMRole.USER

    assert "NEVER INVENT REPRODUCTION STEPS" in system.content
    assert "NEVER ASSERT THAT A BUG EXISTS" in system.content
    assert "sufficient_for_reproduction" in system.content

    assert "Crash in parseConfig" in user.content
    assert "It throws a TypeError." in user.content
    assert "octocat/hello-world" in user.content
    assert "Issue number: 42" in user.content


async def test_request_asks_for_deterministic_json(settings: Settings) -> None:
    analyzer, provider = analyzer_for(payload(), settings)

    await analyzer.analyze(make_issue())

    request = provider.requests[0]
    assert request.expects_json is True
    assert request.temperature == 0.0
    assert request.response_schema is not None
    assert "summary" in request.response_schema["properties"]


async def test_long_bodies_are_truncated_visibly(settings: Settings) -> None:
    analyzer, provider = analyzer_for(
        payload(), settings.model_copy(update={"issue_analysis_body_char_limit": 100})
    )

    await analyzer.analyze(make_issue(body="x" * 5000))

    user = provider.requests[0].messages[1].content
    assert "[Body truncated by ReproGate at 100 characters.]" in user
    assert "x" * 200 not in user


async def test_absent_body_is_stated_rather_than_blank(settings: Settings) -> None:
    analyzer, provider = analyzer_for(payload(), settings)

    await analyzer.analyze(make_issue(body=None))

    assert "(The issue has no body.)" in provider.requests[0].messages[1].content


async def test_analysis_records_provenance(settings: Settings) -> None:
    analyzer, _ = analyzer_for(payload(), settings)

    analysis = await analyzer.analyze(make_issue())

    assert analysis.prompt_version == prompt.PROMPT_ID
    assert analysis.prompt_version == "issue_analysis/v1"
    assert analysis.llm_provider == "fake"
    assert analysis.llm_model == "fake-model-1"
    assert analysis.analyzed_at.tzinfo is not None
    assert analysis.duration_ms >= 0


# --------------------------------------------------------------------------
# Deterministic parsing
# --------------------------------------------------------------------------


async def test_parsing_the_same_reply_twice_is_identical(settings: Settings) -> None:
    analyzer, _ = analyzer_for(COMPLETE_PAYLOAD, settings)

    first = await analyzer.analyze(make_issue())
    second = await analyzer.analyze(make_issue())

    ignore = {"analyzed_at", "duration_ms"}
    assert first.model_dump(exclude=ignore) == second.model_dump(exclude=ignore)


def test_response_schema_is_derived_from_the_validating_model() -> None:
    from app.schemas.issue_analysis import IssueAnalysisPayload
    from app.verification.issue_analyzer import RESPONSE_SCHEMA

    assert IssueAnalysisPayload.model_json_schema() == RESPONSE_SCHEMA


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param('{"a": 1}', {"a": 1}, id="bare"),
        pytest.param('```json\n{"a": 1}\n```', {"a": 1}, id="json-fence"),
        pytest.param('```\n{"a": 1}\n```', {"a": 1}, id="bare-fence"),
        pytest.param('note\n{"a": 1}\nend', {"a": 1}, id="surrounded"),
        pytest.param('  {"a": 1}  ', {"a": 1}, id="whitespace-padded"),
    ],
)
def test_json_extraction_forms(text: str, expected: dict[str, Any]) -> None:
    assert extract_json_object(text) == expected
