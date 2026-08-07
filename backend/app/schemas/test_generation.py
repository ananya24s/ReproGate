"""Input and output models for reproduction test generation.

:class:`VerificationContext` is the *complete* set of facts a generator may
use. Anything not reachable from it does not exist as far as generation is
concerned — that is what makes "never invent repository files" checkable rather
than merely requested.

Nothing here asserts that an issue reproduces. A generated test states what a
run *would* show; only sandbox execution establishes what it does show.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.repository_analysis.models import RepositoryAnalysis, TestFramework
from app.schemas.github import GitHubRepository
from app.schemas.issue_analysis import Confidence, IssueAnalysis


class TestLanguage(str, Enum):
    """A language a reproduction test can be written in."""

    # Dunder names are not turned into enum members, so this opts the class out
    # of pytest collection without affecting the enum itself.
    __test__ = False

    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"


class ExpectedSignal(str, Enum):
    """What a run of the generated test would show if the report is accurate.

    Every value is conditional on purpose: the generator describes an
    expectation, never an observed result.
    """

    FAILS_WHEN_ISSUE_PRESENT = "fails_when_issue_present"
    ERRORS_WHEN_ISSUE_PRESENT = "errors_when_issue_present"
    PASSES_WHEN_ISSUE_PRESENT = "passes_when_issue_present"


class TestGenerationOutcome(str, Enum):
    """How a generation attempt ended."""

    __test__ = False

    GENERATED = "generated"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    UNSUPPORTED_FRAMEWORK = "unsupported_framework"


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


class RelevantFile(BaseModel):
    """A file retrieval proposed, with why it was proposed."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    score: float = Field(default=0.0, ge=0)
    reasons: tuple[str, ...] = ()


class FileSnippet(BaseModel):
    """Source text taken from one repository file."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    content: str
    start_line: int = Field(default=1, ge=1)
    truncated: bool = False


class VerificationContext(BaseModel):
    """Everything a generator is permitted to reason from."""

    model_config = ConfigDict(frozen=True)

    issue_analysis: IssueAnalysis
    repository_analysis: RepositoryAnalysis
    repository: GitHubRepository
    relevant_files: tuple[RelevantFile, ...] = ()
    snippets: tuple[FileSnippet, ...] = ()

    @property
    def available_paths(self) -> frozenset[str]:
        """Every repository path the context exposes.

        A generated test may reference these and nothing else.
        """
        return frozenset(
            [file.path for file in self.relevant_files]
            + [snippet.path for snippet in self.snippets]
        )

    @property
    def has_context(self) -> bool:
        """Whether any repository file was supplied at all."""
        return bool(self.relevant_files or self.snippets)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class RequiredDependency(BaseModel):
    """A package the generated test needs in order to run."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version_spec: str | None = None
    reason: str = Field(min_length=1)
    already_present: bool = False
    """Set by ReproGate from the repository manifest, never by the model."""


class ExpectedOutcome(BaseModel):
    """What a run of the test would demonstrate, stated conditionally."""

    model_config = ConfigDict(frozen=True)

    signal: ExpectedSignal
    description: str = Field(min_length=1)


class GeneratedReproductionTest(BaseModel):
    """A candidate reproduction test. It has not been executed."""

    model_config = ConfigDict(frozen=True)

    language: TestLanguage
    framework: TestFramework
    filename: str = Field(min_length=1)
    source: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    reasoning_summary: str = Field(min_length=1)
    confidence: Confidence
    expected_outcome: ExpectedOutcome
    required_dependencies: tuple[RequiredDependency, ...] = ()
    referenced_files: tuple[str, ...] = ()
    """Context files the test relies on. Always a subset of the context."""


class InsufficientContext(BaseModel):
    """Why a test could not responsibly be written."""

    model_config = ConfigDict(frozen=True)

    reason: str = Field(min_length=1)
    missing: tuple[str, ...] = ()
    """What would have to be supplied for generation to become possible."""

    confidence: Confidence = Confidence.HIGH


class TestGenerationResult(BaseModel):
    """The outcome of one generation attempt, with its provenance."""

    model_config = ConfigDict(frozen=True)

    outcome: TestGenerationOutcome
    test: GeneratedReproductionTest | None = None
    insufficient_context: InsufficientContext | None = None

    prompt_version: str
    llm_provider: str | None = None
    llm_model: str | None = None
    """Absent when a deterministic pre-flight check refused before any call."""

    warnings: tuple[str, ...] = ()
    generated_at: datetime
    duration_ms: int = Field(default=0, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.outcome is TestGenerationOutcome.GENERATED

    @model_validator(mode="after")
    def _outcome_matches_payload(self) -> TestGenerationResult:
        if self.outcome is TestGenerationOutcome.GENERATED and self.test is None:
            raise ValueError("A generated result must carry a test.")
        if (
            self.outcome is not TestGenerationOutcome.GENERATED
            and self.test is not None
        ):
            raise ValueError("Only a generated result may carry a test.")
        return self


# --------------------------------------------------------------------------
# LLM wire shape
# --------------------------------------------------------------------------


class DependencyPayload(BaseModel):
    """A dependency as reported by the model.

    ``already_present`` is deliberately absent: ReproGate resolves it against
    the repository manifest rather than trusting a claim about it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version_spec: str | None = None
    reason: str = Field(min_length=1)


class GeneratedTestPayload(BaseModel):
    """The test object an LLM is asked to produce."""

    model_config = ConfigDict(extra="forbid")

    language: TestLanguage
    framework: TestFramework
    filename: str = Field(min_length=1)
    source: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    reasoning_summary: str = Field(min_length=1)
    confidence: Confidence
    expected_outcome: ExpectedOutcome
    required_dependencies: tuple[DependencyPayload, ...] = ()
    referenced_files: tuple[str, ...] = ()


class TestGenerationPayload(BaseModel):
    """The structured reply an LLM is asked to produce.

    Unknown keys are rejected: a reply that does not match this shape is not
    one we are willing to execute.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["generated", "insufficient_context"]
    test: GeneratedTestPayload | None = None
    insufficient_context: InsufficientContext | None = None

    @model_validator(mode="after")
    def _branch_is_populated(self) -> TestGenerationPayload:
        if self.outcome == "generated" and self.test is None:
            raise ValueError("outcome 'generated' requires a 'test' object.")
        if self.outcome == "insufficient_context" and self.insufficient_context is None:
            raise ValueError(
                "outcome 'insufficient_context' requires an "
                "'insufficient_context' object."
            )
        return self
