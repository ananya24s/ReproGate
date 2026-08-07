"""Structured representation of what a GitHub issue reports.

Two models live here. :class:`IssueAnalysisPayload` is the shape an LLM is
asked to produce; :class:`IssueAnalysis` is the domain result, the payload plus
the provenance needed to reproduce how it was obtained.

Nothing in either model asserts that a defect exists. The analysis describes a
report; deterministic execution decides what is true.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EvidenceBasis(str, Enum):
    """Whether an extracted item came from the issue text or from inference."""

    STATED = "stated"
    """Explicitly present in the issue."""

    INFERRED = "inferred"
    """Concluded by the model from the issue text."""


class Confidence(str, Enum):
    """How much weight an extraction should carry."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IssueField(str, Enum):
    """A part of an issue report that may be absent."""

    SUMMARY = "summary"
    EXPECTED_BEHAVIOR = "expected_behavior"
    OBSERVED_BEHAVIOR = "observed_behavior"
    REPRODUCTION_STEPS = "reproduction_steps"
    ENVIRONMENT = "environment"
    PREREQUISITES = "prerequisites"
    VERSION = "version"
    ERROR_OUTPUT = "error_output"
    CODE_SAMPLE = "code_sample"
    CONFIGURATION = "configuration"


class MentionKind(str, Enum):
    """The kind of artifact an issue names."""

    FILE = "file"
    MODULE = "module"
    FUNCTION = "function"
    CLASS = "class"
    PACKAGE = "package"
    COMMAND = "command"
    CONFIG_KEY = "config_key"
    ERROR_MESSAGE = "error_message"


class ExtractedStatement(BaseModel):
    """A claim taken from the issue, with its basis and supporting quote."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    basis: EvidenceBasis
    confidence: Confidence
    source_quote: str | None = None
    """Verbatim excerpt supporting a stated item; null when inferred."""


class ReproductionStep(BaseModel):
    """One action the issue explicitly describes.

    ``basis`` is constrained to ``stated``: a step the model invented would be
    executed against a real repository, so inference is not permitted here.
    """

    model_config = ConfigDict(frozen=True)

    order: int = Field(ge=1)
    action: str = Field(min_length=1)
    basis: EvidenceBasis
    source_quote: str | None = None


class EnvironmentDetail(BaseModel):
    """A named runtime or configuration fact, such as a version."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    basis: EvidenceBasis


class MentionedEntity(BaseModel):
    """A file, symbol, package, command, or message the issue names."""

    model_config = ConfigDict(frozen=True)

    kind: MentionKind
    value: str = Field(min_length=1)
    basis: EvidenceBasis


class Indicator(BaseModel):
    """A signal about the nature or currency of the report."""

    model_config = ConfigDict(frozen=True)

    signal: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    confidence: Confidence


class MissingInformation(BaseModel):
    """A part of the report that is absent, named explicitly."""

    model_config = ConfigDict(frozen=True)

    field: IssueField
    note: str = Field(min_length=1)


class ReproducibilityAssessment(BaseModel):
    """Whether the report alone supports attempting a reproduction."""

    model_config = ConfigDict(frozen=True)

    sufficient_for_reproduction: bool
    confidence: Confidence
    rationale: str = Field(min_length=1)
    blocking_gaps: tuple[str, ...] = ()


class IssueAnalysisPayload(BaseModel):
    """The structured reply an LLM is asked to produce.

    Unknown keys are rejected rather than ignored: a reply that does not match
    this shape is not one we are willing to build a verification run on.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    expected_behavior: ExtractedStatement | None = None
    observed_behavior: ExtractedStatement | None = None
    reproduction_steps: tuple[ReproductionStep, ...] = ()
    environment: tuple[EnvironmentDetail, ...] = ()
    mentioned_entities: tuple[MentionedEntity, ...] = ()
    prerequisites: tuple[ExtractedStatement, ...] = ()
    configuration_indicators: tuple[Indicator, ...] = ()
    stale_or_fixed_indicators: tuple[Indicator, ...] = ()
    ambiguities: tuple[str, ...] = ()
    missing_information: tuple[MissingInformation, ...] = ()
    reproducibility: ReproducibilityAssessment


class IssueAnalysis(BaseModel):
    """A completed issue analysis, with the provenance that produced it."""

    model_config = ConfigDict(frozen=True)

    issue_number: int = Field(ge=0)
    summary: str
    expected_behavior: ExtractedStatement | None = None
    observed_behavior: ExtractedStatement | None = None
    reproduction_steps: tuple[ReproductionStep, ...] = ()
    environment: tuple[EnvironmentDetail, ...] = ()
    mentioned_entities: tuple[MentionedEntity, ...] = ()
    prerequisites: tuple[ExtractedStatement, ...] = ()
    configuration_indicators: tuple[Indicator, ...] = ()
    stale_or_fixed_indicators: tuple[Indicator, ...] = ()
    ambiguities: tuple[str, ...] = ()
    missing_information: tuple[MissingInformation, ...] = ()
    reproducibility: ReproducibilityAssessment

    # -- Provenance --------------------------------------------------------
    prompt_version: str
    llm_provider: str
    llm_model: str
    warnings: tuple[str, ...] = ()
    """Problems ReproGate corrected in the reply, such as discarded steps."""

    analyzed_at: datetime
    duration_ms: int = Field(default=0, ge=0)

    @property
    def has_reproduction_steps(self) -> bool:
        """Whether the issue supplied any usable reproduction steps."""
        return bool(self.reproduction_steps)
