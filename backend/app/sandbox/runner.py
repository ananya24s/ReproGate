"""Runs a generated test inside a sandboxed container and captures its result.

The lifecycle is fixed: build an isolated workspace, inject the generated test,
start a confined container, install dependencies with network, cut the network,
run only that one test, capture everything, and destroy both the container and
the workspace — on success, failure, timeout, or cancellation alike.

This module reports what happened. It never decides what the result means for
the reported issue.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.exceptions import SandboxError, WorkspaceError
from app.core.logging import get_logger
from app.repository_analysis.models import (
    PackageManager,
    RepositoryAnalysis,
    TestFramework,
)
from app.sandbox.docker_client import ContainerHandle, SandboxDockerClient
from app.sandbox.limits import SandboxLimits
from app.sandbox.models import (
    CleanupReport,
    CleanupStatus,
    ExecOutcome,
    ExecutionPhase,
    InfrastructureStatus,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    TestReport,
    TestStatus,
    TimeoutInfo,
)
from app.schemas.test_generation import GeneratedReproductionTest

logger = get_logger(__name__)

#: Never copied into the execution workspace: history is irrelevant to a test
#: run, and vendored dependencies are reinstalled from the manifest.
_EXCLUDED_FROM_COPY: Final = shutil.ignore_patterns(
    ".git", "node_modules", ".venv", "__pycache__", ".pnpm-store"
)

#: Where the runner writes its machine-readable report, inside the workspace.
_REPORT_FILENAME: Final = ".reprogate-report.json"

#: Only these produce a run. Anything else is refused before a container exists.
SUPPORTED_FRAMEWORKS: Final = frozenset({TestFramework.JEST, TestFramework.VITEST})
SUPPORTED_PACKAGE_MANAGERS: Final = frozenset(
    {PackageManager.NPM, PackageManager.PNPM, PackageManager.YARN}
)

#: `corepack enable` provisions pnpm and yarn; npm ships with the image.
_COREPACK_MANAGERS: Final = frozenset({PackageManager.PNPM, PackageManager.YARN})

_INSTALL_COMMANDS: Final[dict[PackageManager, tuple[str, ...]]] = {
    PackageManager.NPM: ("npm", "install", "--no-audit", "--no-fund"),
    PackageManager.PNPM: ("pnpm", "install", "--no-frozen-lockfile"),
    PackageManager.YARN: ("yarn", "install"),
}

#: How each manager runs a locally installed binary.
_EXEC_PREFIXES: Final[dict[PackageManager, tuple[str, ...]]] = {
    PackageManager.NPM: ("npx", "--no-install"),
    PackageManager.PNPM: ("pnpm", "exec"),
    PackageManager.YARN: ("yarn", "run"),
}

_RUN_ID_ALLOWED: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class SandboxRunner:
    """Executes one generated reproduction test in a disposable container."""

    def __init__(
        self,
        docker_client: SandboxDockerClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._docker = docker_client or SandboxDockerClient(settings=self._settings)
        self._workspace_root = Path(self._settings.sandbox_workspace_root).expanduser()
        self._container_path = self._settings.sandbox_workspace_container_path
        self._image = self._settings.sandbox_node_image

    async def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        """Execute ``request`` and return structured evidence.

        Never raises for an experiment that fails or a test that does not pass.
        Infrastructure problems are reported through
        ``infrastructure_status``, not exceptions.
        """
        started_at = datetime.now(tz=UTC)
        started = time.monotonic()
        limits = request.limits or SandboxLimits.from_settings(self._settings)
        analysis = request.repository_analysis
        test = request.generated_test

        state = _RunState(
            run_id=request.run_id,
            image=self._image,
            test_path=test.filename,
            limits=limits,
            started_at=started_at,
            started=started,
            package_manager=analysis.package_manager,
            framework=analysis.test_framework,
        )

        unsupported = _check_supported(analysis)
        if unsupported is not None:
            return state.failure(InfrastructureStatus.INFRASTRUCTURE_ERROR, unsupported)

        workspace: Path | None = None
        handle: ContainerHandle | None = None
        result: SandboxExecutionResult

        try:
            try:
                workspace = self._prepare_workspace(request)
                state.workspace = workspace
                _inject_test(workspace, test)

                handle = await self._start_container(workspace, limits)
                state.handle = handle

                result = await self._execute(state, analysis, test)
            except WorkspaceError as exc:
                result = state.failure(InfrastructureStatus.WORKSPACE_FAILED, str(exc))
            except SandboxError as exc:
                status = (
                    InfrastructureStatus.CONTAINER_START_FAILED
                    if handle is None
                    else InfrastructureStatus.INFRASTRUCTURE_ERROR
                )
                result = state.failure(status, str(exc))
            except Exception as exc:
                logger.exception(
                    "Unexpected sandbox failure", extra={"run_id": request.run_id}
                )
                result = state.failure(
                    InfrastructureStatus.INFRASTRUCTURE_ERROR,
                    f"Unexpected failure: {exc}",
                )
        finally:
            # Runs on success, failure, timeout, and cancellation alike. On
            # cancellation the exception propagates from here, after cleanup.
            cleanup = await self._cleanup(handle, workspace)

        # Attached after the fact so the result always reports what cleanup
        # actually achieved, rather than what it was expected to.
        return result.model_copy(update={"cleanup": cleanup})

    # -- Lifecycle ---------------------------------------------------------

    def _prepare_workspace(self, request: SandboxExecutionRequest) -> Path:
        """Copy the clone into a fresh per-run workspace.

        The clone is copied rather than mounted so a run cannot mutate the
        source checkout, and so exactly one disposable directory is exposed.
        """
        run_id = request.run_id
        if not run_id or not set(run_id) <= _RUN_ID_ALLOWED:
            raise WorkspaceError(f"{run_id!r} is not a usable execution identifier.")

        source = Path(request.repository_path).expanduser()
        if not source.is_dir():
            raise WorkspaceError(f"{source} is not a directory that can be executed.")
        source = source.resolve()

        try:
            root = self._workspace_root.resolve()
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"The workspace root {self._workspace_root} could not be created."
            ) from exc

        workspace = (root / f"exec-{run_id}").resolve()
        if workspace == root or not workspace.is_relative_to(root):
            raise WorkspaceError(
                f"The execution workspace for {run_id!r} would fall outside {root}."
            )

        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)

        try:
            shutil.copytree(
                source, workspace, ignore=_EXCLUDED_FROM_COPY, symlinks=False
            )
        except OSError as exc:
            raise WorkspaceError(
                f"The repository could not be copied into {workspace}: {exc}"
            ) from exc

        logger.info(
            "Prepared execution workspace",
            extra={"run_id": run_id, "workspace": str(workspace)},
        )
        return workspace

    async def _start_container(
        self, workspace: Path, limits: SandboxLimits
    ) -> ContainerHandle:
        """Pull the image if needed, then create and start the container."""
        await self._docker.ensure_image(self._image)

        handle = await self._docker.create_container(
            image=self._image,
            workspace_host_path=str(workspace),
            workspace_container_path=self._container_path,
            limits=limits,
            # A dead-man timer: even if ReproGate crashes, the container exits.
            command=["sleep", str(limits.total_timeout_seconds)],
            environment=_container_environment(),
        )
        await self._docker.start(handle)
        return handle

    async def _execute(
        self,
        state: _RunState,
        analysis: RepositoryAnalysis,
        test: GeneratedReproductionTest,
    ) -> SandboxExecutionResult:
        """Install dependencies, cut the network, then run the one test."""
        limits = state.limits
        handle = state.handle
        assert handle is not None  # set by the caller
        manager = analysis.package_manager
        assert manager is not None  # checked by _check_supported

        if manager in _COREPACK_MANAGERS:
            # Provisions pnpm/yarn; failure here is an install failure.
            prep = await self._docker.exec(
                handle,
                ["corepack", "enable"],
                workdir=self._container_path,
                timeout=float(limits.install_timeout_seconds),
                max_output_bytes=limits.max_output_bytes,
            )
            if prep.timed_out:
                return state.timeout(ExecutionPhase.INSTALL, prep)

        install_command = list(_INSTALL_COMMANDS[manager])
        state.install_command = tuple(install_command)
        install = await self._docker.exec(
            handle,
            install_command,
            workdir=self._container_path,
            timeout=float(limits.install_timeout_seconds),
            max_output_bytes=limits.max_output_bytes,
        )
        state.install = install

        if install.timed_out:
            return state.timeout(ExecutionPhase.INSTALL, install)
        if not install.succeeded:
            return state.failure(
                InfrastructureStatus.INSTALL_FAILED,
                f"Dependency installation exited with code {install.exit_code}.",
            )
        state.dependencies_installed = True

        if not limits.network_enabled_during_test:
            state.network_disabled = await self._docker.disable_network(handle)

        test_command = _build_test_command(
            manager, analysis.test_framework, test.filename
        )
        state.test_command = tuple(test_command)
        outcome = await self._docker.exec(
            handle,
            list(test_command),
            workdir=self._container_path,
            timeout=float(limits.test_timeout_seconds),
            max_output_bytes=limits.max_output_bytes,
        )
        state.test = outcome

        if outcome.timed_out:
            return state.timeout(ExecutionPhase.TEST, outcome)

        state.report = _read_report(state.workspace)
        return state.completed()

    async def _cleanup(
        self, handle: ContainerHandle | None, workspace: Path | None
    ) -> CleanupReport:
        """Destroy the container and the workspace. Best effort, always run."""
        details: list[str] = []
        container_removed = handle is None
        workspace_removed = workspace is None

        if handle is not None:
            try:
                await self._docker.kill(handle)
                await self._docker.remove(handle)
                container_removed = True
            except Exception as exc:
                details.append(f"container {handle.short_id}: {exc}")

        if workspace is not None:
            removed, error = self._remove_workspace(workspace)
            workspace_removed = removed
            if error:
                details.append(error)

        if container_removed and workspace_removed:
            status = CleanupStatus.COMPLETED
        elif container_removed or workspace_removed:
            status = CleanupStatus.PARTIAL
        else:
            status = CleanupStatus.FAILED

        logger.info(
            "Sandbox cleanup finished",
            extra={
                "status": status.value,
                "container_removed": container_removed,
                "workspace_removed": workspace_removed,
                "detail_count": len(details),
            },
        )
        return CleanupReport(
            status=status,
            container_removed=container_removed,
            workspace_removed=workspace_removed,
            details=tuple(details),
        )

    def _remove_workspace(self, workspace: Path) -> tuple[bool, str | None]:
        """Delete a workspace, refusing any path outside the workspace root."""
        try:
            root = self._workspace_root.resolve()
            resolved = workspace.resolve()
        except OSError as exc:  # pragma: no cover - resolution is not expected to fail
            return False, f"workspace {workspace}: {exc}"

        if resolved == root or not resolved.is_relative_to(root):
            return False, f"refused to remove {resolved}, which is outside {root}"

        if not resolved.exists():
            return True, None

        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            return False, f"workspace {resolved} could not be removed"
        return True, None


# --------------------------------------------------------------------------
# Preparation helpers
# --------------------------------------------------------------------------


def _check_supported(analysis: RepositoryAnalysis) -> str | None:
    """Reject a repository this module cannot execute, before any container."""
    if analysis.test_framework not in SUPPORTED_FRAMEWORKS:
        named = (
            analysis.test_framework.value
            if analysis.test_framework
            else "none detected"
        )
        return f"Unsupported test framework: {named}."

    if analysis.package_manager not in SUPPORTED_PACKAGE_MANAGERS:
        named = (
            analysis.package_manager.value
            if analysis.package_manager
            else "none detected"
        )
        return f"Unsupported package manager: {named}."

    return None


def _inject_test(workspace: Path, test: GeneratedReproductionTest) -> None:
    """Write the generated test at its declared path inside the workspace.

    The path is re-validated here even though generation validated it: this
    module must not trust its input, and a write outside the workspace would
    escape the sandbox before the container even starts.
    """
    filename = test.filename
    if filename.startswith("/") or ".." in PurePosixPath(filename).parts:
        raise WorkspaceError(
            f"The generated test path {filename!r} is not a safe relative path."
        )

    root = workspace.resolve()
    destination = (root / filename).resolve()
    if destination == root or not destination.is_relative_to(root):
        raise WorkspaceError(
            f"The generated test path {filename!r} resolves outside the workspace."
        )

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(test.source, encoding="utf-8")
    except OSError as exc:
        raise WorkspaceError(
            f"The generated test could not be written to {destination}: {exc}"
        ) from exc

    logger.info(
        "Injected generated test",
        extra={"path": filename, "source_chars": len(test.source)},
    )


def _build_test_command(
    manager: PackageManager, framework: TestFramework | None, test_path: str
) -> tuple[str, ...]:
    """Compose the argv that runs exactly one test file.

    The path is a separate argv element, never spliced into a string, so it
    cannot be read as anything but an operand.
    """
    prefix = _EXEC_PREFIXES[manager]

    if framework is TestFramework.JEST:
        # `--runTestsByPath` takes exact paths rather than patterns.
        return (
            *prefix,
            "jest",
            "--ci",
            "--runTestsByPath",
            test_path,
            "--json",
            f"--outputFile={_REPORT_FILENAME}",
        )

    return (
        *prefix,
        "vitest",
        "run",
        "--reporter=json",
        f"--outputFile={_REPORT_FILENAME}",
        test_path,
    )


def _container_environment() -> dict[str, str]:
    """A minimal, non-interactive environment for the container."""
    return {
        "CI": "true",
        "NODE_ENV": "test",
        "HOME": "/tmp",
        "npm_config_cache": "/tmp/.npm",
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        # Berry refuses to install when the lockfile would change; the sandbox
        # may legitimately need the generated test's dependencies.
        "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    }


def _read_report(workspace: Path | None) -> TestReport:
    """Read the runner's JSON report, which decides passed vs failed.

    Using the runner's own counts rather than the exit code is what separates
    "tests ran and some failed" from "the runner crashed" — both exit non-zero.
    """
    if workspace is None:
        return TestReport()

    path = workspace / _REPORT_FILENAME
    try:
        if not path.is_file():
            return TestReport()
        payload: Any = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return TestReport()

    if not isinstance(payload, dict):
        return TestReport()

    return TestReport(
        available=True,
        tests_run=_as_int(payload.get("numTotalTests")),
        tests_passed=_as_int(payload.get("numPassedTests")),
        tests_failed=_as_int(payload.get("numFailedTests")),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


# --------------------------------------------------------------------------
# Run state
# --------------------------------------------------------------------------


class _RunState:
    """Accumulates a run's facts so every exit path builds the same result."""

    def __init__(
        self,
        *,
        run_id: str,
        image: str,
        test_path: str,
        limits: SandboxLimits,
        started_at: datetime,
        started: float,
        package_manager: PackageManager | None,
        framework: TestFramework | None,
    ) -> None:
        self.run_id = run_id
        self.image = image
        self.test_path = test_path
        self.limits = limits
        self.started_at = started_at
        self.started = started
        self.package_manager = package_manager
        self.framework = framework

        self.workspace: Path | None = None
        self.handle: ContainerHandle | None = None
        self.install_command: tuple[str, ...] = ()
        self.test_command: tuple[str, ...] = ()
        self.install: ExecOutcome | None = None
        self.test: ExecOutcome | None = None
        self.report = TestReport()
        self.dependencies_installed = False
        self.network_disabled = False
        self.cleanup = CleanupReport(status=CleanupStatus.COMPLETED)

    def completed(self) -> SandboxExecutionResult:
        """The experiment ran to a verdict."""
        return self._build(
            InfrastructureStatus.COMPLETED,
            _classify(self.test, self.report),
            None,
            None,
        )

    def failure(
        self, status: InfrastructureStatus, reason: str
    ) -> SandboxExecutionResult:
        """ReproGate could not run the experiment."""
        logger.warning(
            "Sandbox execution failed",
            extra={
                "run_id": self.run_id,
                "infrastructure_status": status.value,
                "reason": reason,
            },
        )
        return self._build(status, TestStatus.NOT_RUN, None, reason)

    def timeout(
        self, phase: ExecutionPhase, outcome: ExecOutcome
    ) -> SandboxExecutionResult:
        """A phase exceeded its limit and the container was killed."""
        limit = (
            self.limits.install_timeout_seconds
            if phase is ExecutionPhase.INSTALL
            else self.limits.test_timeout_seconds
        )
        status = (
            InfrastructureStatus.INSTALL_TIMEOUT
            if phase is ExecutionPhase.INSTALL
            else InfrastructureStatus.TEST_TIMEOUT
        )
        info = TimeoutInfo(
            phase=phase,
            limit_seconds=limit,
            elapsed_seconds=outcome.duration_ms / 1000,
        )
        return self._build(
            status,
            TestStatus.NOT_RUN,
            info,
            f"The {phase.value} phase exceeded its {limit}s limit.",
        )

    def _build(
        self,
        infrastructure_status: InfrastructureStatus,
        test_status: TestStatus,
        timeout: TimeoutInfo | None,
        failure_reason: str | None,
    ) -> SandboxExecutionResult:
        completed_at = datetime.now(tz=UTC)
        install = self.install
        test = self.test

        return SandboxExecutionResult(
            run_id=self.run_id,
            infrastructure_status=infrastructure_status,
            test_status=test_status,
            install_command=self.install_command,
            test_command=self.test_command,
            install_exit_code=install.exit_code if install else None,
            test_exit_code=test.exit_code if test else None,
            stdout=test.stdout if test else "",
            stderr=test.stderr if test else "",
            install_stdout=install.stdout if install else "",
            install_stderr=install.stderr if install else "",
            started_at=self.started_at,
            completed_at=completed_at,
            install_duration_ms=install.duration_ms if install else 0,
            test_duration_ms=test.duration_ms if test else 0,
            total_duration_ms=int((time.monotonic() - self.started) * 1000),
            container_image=self.image,
            container_id=self.handle.id if self.handle else None,
            container_created=self.handle is not None,
            dependencies_installed=self.dependencies_installed,
            network_disabled_for_test=self.network_disabled,
            generated_test_path=self.test_path,
            package_manager=(
                self.package_manager.value if self.package_manager else None
            ),
            test_framework=self.framework.value if self.framework else None,
            timeout=timeout,
            report=self.report,
            cleanup=self.cleanup,
            failure_reason=failure_reason,
        )


def _classify(outcome: ExecOutcome | None, report: TestReport) -> TestStatus:
    """Decide what the test showed.

    The runner's own report is authoritative when present, because a non-zero
    exit means either "assertions failed" or "the runner crashed" and only the
    report distinguishes them.
    """
    if outcome is None:
        return TestStatus.NOT_RUN

    if report.available:
        if report.tests_failed > 0:
            return TestStatus.FAILED
        if report.tests_run > 0:
            return TestStatus.PASSED
        # The runner finished but collected nothing to assert on.
        return TestStatus.ERRORED

    # Without a report a non-zero exit cannot be told apart from a crash, so it
    # is reported as an error rather than guessed at.
    return TestStatus.PASSED if outcome.exit_code == 0 else TestStatus.ERRORED
