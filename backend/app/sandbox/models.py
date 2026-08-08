"""Internal data structures describing sandbox execution requests and results.

Two outcomes are modelled separately and must not be conflated. The
*infrastructure* outcome says whether ReproGate managed to run the experiment;
the *test* outcome says what the experiment showed. A reproduction test that
fails is a successful execution — very often the point of the run.

Nothing here interprets what a result means for the reported issue.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.repository_analysis.models import RepositoryAnalysis
from app.sandbox.limits import SandboxLimits
from app.schemas.test_generation import GeneratedReproductionTest


class ExecutionPhase(str, Enum):
    """A stage of the execution lifecycle."""

    WORKSPACE = "workspace"
    CONTAINER_START = "container_start"
    INSTALL = "install"
    TEST = "test"


class InfrastructureStatus(str, Enum):
    """Whether ReproGate was able to run the experiment at all."""

    COMPLETED = "completed"
    """The container ran, dependencies installed, and the test produced a verdict."""

    WORKSPACE_FAILED = "workspace_failed"
    CONTAINER_START_FAILED = "container_start_failed"
    INSTALL_FAILED = "install_failed"
    INSTALL_TIMEOUT = "install_timeout"
    TEST_TIMEOUT = "test_timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


class TestStatus(str, Enum):
    """What the generated reproduction test showed."""

    # Dunder names are not turned into enum members, so this opts the class out
    # of pytest collection without affecting the enum itself.
    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    """The runner started but could not produce a verdict — a crash, a syntax
    error, or no collected tests."""

    NOT_RUN = "not_run"


class CleanupStatus(str, Enum):
    """How completely the run's resources were released."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class TimeoutInfo(BaseModel):
    """Which phase exceeded its limit."""

    model_config = ConfigDict(frozen=True)

    phase: ExecutionPhase
    limit_seconds: int = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)


class CleanupReport(BaseModel):
    """What was released, and what resisted."""

    model_config = ConfigDict(frozen=True)

    status: CleanupStatus
    container_removed: bool = False
    workspace_removed: bool = False
    details: tuple[str, ...] = ()


class TestReport(BaseModel):
    """Counts read from the runner's own JSON report."""

    __test__ = False

    model_config = ConfigDict(frozen=True)

    available: bool = False
    """False when the runner produced no machine-readable report."""

    tests_run: int = Field(default=0, ge=0)
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)


class ExecOutcome(BaseModel):
    """The result of one command run inside the container."""

    model_config = ConfigDict(frozen=True)

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = Field(default=0, ge=0)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxExecutionRequest(BaseModel):
    """Everything needed to execute one generated test."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str = Field(min_length=1)
    repository_path: Path
    """The cloned repository. It is copied, never mounted directly."""

    repository_analysis: RepositoryAnalysis
    generated_test: GeneratedReproductionTest
    limits: SandboxLimits | None = None


class SandboxExecutionResult(BaseModel):
    """Structured evidence of one sandboxed execution.

    This records what happened. It draws no conclusion about the reported
    issue; that belongs to evidence building and classification.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str

    # -- Outcomes, kept separate -------------------------------------------
    infrastructure_status: InfrastructureStatus
    test_status: TestStatus

    # -- Commands ----------------------------------------------------------
    install_command: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    install_exit_code: int | None = None
    test_exit_code: int | None = None

    # -- Output ------------------------------------------------------------
    stdout: str = ""
    stderr: str = ""
    install_stdout: str = ""
    install_stderr: str = ""

    # -- Timing ------------------------------------------------------------
    started_at: datetime
    completed_at: datetime
    install_duration_ms: int = Field(default=0, ge=0)
    test_duration_ms: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)

    # -- Environment -------------------------------------------------------
    container_image: str
    container_id: str | None = None
    container_created: bool = False
    dependencies_installed: bool = False
    network_disabled_for_test: bool = False
    generated_test_path: str
    """Repository-relative path the test was injected at."""

    package_manager: str | None = None
    test_framework: str | None = None

    # -- Diagnostics -------------------------------------------------------
    timeout: TimeoutInfo | None = None
    report: TestReport = TestReport()
    cleanup: CleanupReport
    failure_reason: str | None = None
    """Set only for infrastructure failures, never for a failing test."""

    @property
    def executed(self) -> bool:
        """Whether the experiment ran to a verdict."""
        return self.infrastructure_status is InfrastructureStatus.COMPLETED

    @property
    def infrastructure_ok(self) -> bool:
        """Whether ReproGate's side of the run worked.

        A failing reproduction test leaves this true — that is the distinction
        this module exists to preserve.
        """
        return self.infrastructure_status is InfrastructureStatus.COMPLETED
