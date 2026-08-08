"""Unit tests for sandboxed execution of a generated reproduction test.

The Docker SDK is faked, so these run without a daemon. Faking at the SDK
boundary rather than at ``SandboxDockerClient`` is deliberate: it leaves the
real host-config assembly under test, which is where the security controls
live.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from app.repository_analysis.models import (
    Language,
    LanguageUsage,
    NodePackage,
    PackageManager,
    RepositoryAnalysis,
    TestFramework,
)
from app.sandbox import (
    CleanupStatus,
    ExecutionPhase,
    InfrastructureStatus,
    SandboxDockerClient,
    SandboxExecutionRequest,
    SandboxLimits,
    SandboxRunner,
    TestStatus,
)
from app.schemas.issue_analysis import Confidence
from app.schemas.test_generation import (
    ExpectedOutcome,
    ExpectedSignal,
    GeneratedReproductionTest,
    TestLanguage,
)

# --------------------------------------------------------------------------
# Docker SDK fake
# --------------------------------------------------------------------------


class FakeExecResult:
    """A scripted response for one command run inside the container."""

    def __init__(
        self,
        exit_code: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        *,
        delay: float = 0.0,
        report: dict[str, Any] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.delay = delay
        self.report = report


class FakeContainer:
    def __init__(self, container_id: str, sdk: FakeDockerSDK) -> None:
        self.id = container_id
        self._sdk = sdk
        self.started = False
        self.killed = False
        self.removed = False
        self.remove_kwargs: dict[str, Any] = {}
        self.attrs: dict[str, Any] = {"NetworkSettings": {"Networks": {"bridge": {}}}}

    def start(self) -> None:
        if self._sdk.start_error is not None:
            raise self._sdk.start_error
        self.started = True

    def exec_run(self, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        argv = kwargs["cmd"]
        self._sdk.exec_calls.append(kwargs)

        if not isinstance(argv, list):
            raise AssertionError(f"exec_run received a non-argv command: {argv!r}")

        result = self._sdk.resolve(argv)
        if result.delay:
            time.sleep(result.delay)
        if result.report is not None and self._sdk.workspace is not None:
            (self._sdk.workspace / ".reprogate-report.json").write_text(
                json.dumps(result.report)
            )
        return result.exit_code, (result.stdout, result.stderr)

    def reload(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def remove(self, **kwargs: Any) -> None:
        if self._sdk.remove_error is not None:
            raise self._sdk.remove_error
        self.removed = True
        self.remove_kwargs = kwargs


class FakeNetwork:
    def __init__(self, name: str, sdk: FakeDockerSDK) -> None:
        self.name = name
        self._sdk = sdk

    def disconnect(self, container: Any, **kwargs: Any) -> None:
        self._sdk.disconnected.append(self.name)


class FakeImages:
    def __init__(self, sdk: FakeDockerSDK) -> None:
        self._sdk = sdk

    def get(self, image: str) -> Any:
        if image in self._sdk.local_images:
            return object()
        raise RuntimeError("image not found locally")

    def pull(self, image: str) -> Any:
        self._sdk.pulled.append(image)
        self._sdk.local_images.add(image)
        return object()


class FakeContainers:
    def __init__(self, sdk: FakeDockerSDK) -> None:
        self._sdk = sdk

    def create(self, **kwargs: Any) -> FakeContainer:
        self._sdk.create_kwargs = kwargs
        if self._sdk.create_error is not None:
            raise self._sdk.create_error
        container = FakeContainer("c" * 64, self._sdk)
        self._sdk.container = container
        return container


class FakeNetworks:
    def __init__(self, sdk: FakeDockerSDK) -> None:
        self._sdk = sdk

    def get(self, name: str) -> FakeNetwork:
        return FakeNetwork(name, self._sdk)


class FakeDockerSDK:
    """A stand-in for ``docker.DockerClient`` covering only what we call."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.images = FakeImages(self)
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks(self)

        self.local_images: set[str] = {"node:20-bookworm-slim"}
        self.pulled: list[str] = []
        self.exec_calls: list[dict[str, Any]] = []
        self.disconnected: list[str] = []
        self.create_kwargs: dict[str, Any] = {}
        self.container: FakeContainer | None = None
        self.workspace = workspace

        self.create_error: Exception | None = None
        self.start_error: Exception | None = None
        self.remove_error: Exception | None = None

        self.script: list[tuple[str, FakeExecResult]] = []
        self.default = FakeExecResult()

    def on(self, marker: str, result: FakeExecResult) -> FakeDockerSDK:
        """Script a response for any command whose argv contains ``marker``."""
        self.script.append((marker, result))
        return self

    def resolve(self, argv: list[str]) -> FakeExecResult:
        for marker, result in self.script:
            if marker in argv:
                return result
        return self.default

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


PASSING_REPORT = {"numTotalTests": 1, "numPassedTests": 1, "numFailedTests": 0}
FAILING_REPORT = {"numTotalTests": 1, "numPassedTests": 0, "numFailedTests": 1}
EMPTY_REPORT = {"numTotalTests": 0, "numPassedTests": 0, "numFailedTests": 0}


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def settings(workspace_root: Path) -> Settings:
    return Settings(
        sandbox_workspace_root=str(workspace_root),
        sandbox_install_timeout_seconds=5,
        sandbox_timeout_seconds=5,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A cloned repository, including things that must not be copied."""
    root = tmp_path / "clone"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "w"}))
    (root / "src" / "parser.ts").write_text("export const parse = () => {};\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("// vendored\n")
    return root


def make_analysis(
    framework: TestFramework | None = TestFramework.VITEST,
    manager: PackageManager | None = PackageManager.NPM,
) -> RepositoryAnalysis:
    return RepositoryAnalysis(
        root="/tmp/clone",
        languages=(LanguageUsage(language=Language.TYPESCRIPT, file_count=2),),
        primary_language=Language.TYPESCRIPT,
        uses_typescript=True,
        is_node_project=True,
        root_package=NodePackage(path="package.json", directory=".", name="w"),
        package_manager=manager,
        test_framework=framework,
        analyzed_at=datetime.now(tz=UTC),
    )


def make_test(filename: str = "src/parser.repro.test.ts") -> GeneratedReproductionTest:
    return GeneratedReproductionTest(
        language=TestLanguage.TYPESCRIPT,
        framework=TestFramework.VITEST,
        filename=filename,
        source=(
            "import { it, expect } from 'vitest';\nit('x', () => expect(1).toBe(1));\n"
        ),
        reasoning_summary="Checks the reported call.",
        confidence=Confidence.MEDIUM,
        expected_outcome=ExpectedOutcome(
            signal=ExpectedSignal.FAILS_WHEN_ISSUE_PRESENT,
            description="Would fail if the issue is present.",
        ),
    )


def make_request(
    repository: Path,
    *,
    run_id: str = "run-0001",
    framework: TestFramework | None = TestFramework.VITEST,
    manager: PackageManager | None = PackageManager.NPM,
    filename: str = "src/parser.repro.test.ts",
    limits: SandboxLimits | None = None,
) -> SandboxExecutionRequest:
    return SandboxExecutionRequest(
        run_id=run_id,
        repository_path=repository,
        repository_analysis=make_analysis(framework, manager),
        generated_test=make_test(filename),
        limits=limits,
    )


def runner_for(sdk: FakeDockerSDK, settings: Settings) -> SandboxRunner:
    return SandboxRunner(
        SandboxDockerClient(docker_client=sdk, settings=settings), settings
    )


@pytest.fixture
def sdk(workspace_root: Path) -> FakeDockerSDK:
    # The runner writes into `exec-<run_id>`; the fake writes its report there.
    return FakeDockerSDK(workspace=workspace_root / "exec-run-0001")


# --------------------------------------------------------------------------
# Successful execution
# --------------------------------------------------------------------------


async def test_successful_execution(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.on("vitest", FakeExecResult(0, b"1 passed", report=PASSING_REPORT))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.test_status is TestStatus.PASSED
    assert result.infrastructure_ok is True
    assert result.container_created is True
    assert result.dependencies_installed is True
    assert result.install_exit_code == 0
    assert result.test_exit_code == 0
    assert result.container_image == "node:20-bookworm-slim"
    assert result.generated_test_path == "src/parser.repro.test.ts"
    assert result.package_manager == "npm"
    assert result.test_framework == "vitest"
    assert result.report.available is True
    assert (result.report.tests_run, result.report.tests_passed) == (1, 1)
    assert result.started_at <= result.completed_at
    assert result.timeout is None
    assert result.failure_reason is None


async def test_workspace_excludes_history_and_vendored_dependencies(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    copied: dict[str, bool] = {}

    original = sdk.resolve

    def capture(argv: list[str]) -> FakeExecResult:
        workspace = workspace_root / "exec-run-0001"
        copied["git"] = (workspace / ".git").exists()
        copied["node_modules"] = (workspace / "node_modules").exists()
        copied["source"] = (workspace / "src" / "parser.ts").exists()
        copied["test"] = (workspace / "src" / "parser.repro.test.ts").exists()
        return original(argv)

    sdk.resolve = capture  # type: ignore[method-assign]

    await runner_for(sdk, settings).run(make_request(repository))

    assert copied["source"] is True
    assert copied["test"] is True, "the generated test must be injected"
    assert copied["git"] is False
    assert copied["node_modules"] is False


async def test_source_repository_is_not_mutated(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    await runner_for(sdk, settings).run(make_request(repository))

    assert not (repository / "src" / "parser.repro.test.ts").exists()
    assert (repository / ".git").exists()


# --------------------------------------------------------------------------
# Test outcomes, kept apart from infrastructure
# --------------------------------------------------------------------------


async def test_failing_reproduction_test_is_not_an_infrastructure_failure(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.on("vitest", FakeExecResult(1, b"1 failed", report=FAILING_REPORT))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.test_status is TestStatus.FAILED
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.infrastructure_ok is True
    assert result.failure_reason is None
    assert result.test_exit_code == 1
    assert result.report.tests_failed == 1


async def test_runner_crash_is_reported_as_errored(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    # A crash exits non-zero and writes no report — indistinguishable from a
    # test failure by exit code alone, which is why the report is authoritative.
    sdk.on("vitest", FakeExecResult(1, b"", b"SyntaxError: Unexpected token"))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.test_status is TestStatus.ERRORED
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.report.available is False
    assert "SyntaxError" in result.stderr


async def test_runner_collecting_no_tests_is_errored(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.on("vitest", FakeExecResult(1, b"no tests", report=EMPTY_REPORT))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.test_status is TestStatus.ERRORED
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED


# --------------------------------------------------------------------------
# Infrastructure failures
# --------------------------------------------------------------------------


async def test_dependency_installation_failure(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.on("install", FakeExecResult(1, b"", b"ERESOLVE could not resolve"))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.infrastructure_status is InfrastructureStatus.INSTALL_FAILED
    assert result.test_status is TestStatus.NOT_RUN
    assert result.dependencies_installed is False
    assert result.install_exit_code == 1
    assert "ERESOLVE" in result.install_stderr
    assert result.test_command == ()
    assert result.failure_reason is not None


async def test_installation_timeout(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    limits = SandboxLimits.from_settings(settings).model_copy(
        update={"install_timeout_seconds": 1}
    )
    sdk.on("install", FakeExecResult(0, delay=3.0))

    result = await runner_for(sdk, settings).run(
        make_request(repository, limits=limits)
    )

    assert result.infrastructure_status is InfrastructureStatus.INSTALL_TIMEOUT
    assert result.test_status is TestStatus.NOT_RUN
    assert result.timeout is not None
    assert result.timeout.phase is ExecutionPhase.INSTALL
    assert result.timeout.limit_seconds == 1
    assert sdk.container is not None and sdk.container.killed is True


async def test_test_timeout(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    limits = SandboxLimits.from_settings(settings).model_copy(
        update={"test_timeout_seconds": 1}
    )
    sdk.on("vitest", FakeExecResult(0, delay=3.0))

    result = await runner_for(sdk, settings).run(
        make_request(repository, limits=limits)
    )

    assert result.infrastructure_status is InfrastructureStatus.TEST_TIMEOUT
    assert result.test_status is TestStatus.NOT_RUN
    assert result.timeout is not None
    assert result.timeout.phase is ExecutionPhase.TEST
    assert result.dependencies_installed is True


async def test_container_creation_failure(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.create_error = RuntimeError("no such image")

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.infrastructure_status is InfrastructureStatus.CONTAINER_START_FAILED
    assert result.test_status is TestStatus.NOT_RUN
    assert result.container_created is False
    assert result.failure_reason is not None


async def test_container_start_failure(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.start_error = RuntimeError("OCI runtime create failed")

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.infrastructure_status is InfrastructureStatus.CONTAINER_START_FAILED
    assert result.test_status is TestStatus.NOT_RUN


async def test_missing_repository_is_a_workspace_failure(
    sdk: FakeDockerSDK, settings: Settings, tmp_path: Path
) -> None:
    result = await runner_for(sdk, settings).run(
        make_request(tmp_path / "does-not-exist")
    )

    assert result.infrastructure_status is InfrastructureStatus.WORKSPACE_FAILED
    assert result.test_status is TestStatus.NOT_RUN


@pytest.mark.parametrize(
    ("framework", "manager"),
    [
        pytest.param(None, PackageManager.NPM, id="no-framework"),
        pytest.param(TestFramework.VITEST, None, id="no-package-manager"),
        pytest.param(TestFramework.VITEST, PackageManager.BUN, id="bun-unsupported"),
    ],
)
async def test_unsupported_repository_is_refused_before_any_container(
    sdk: FakeDockerSDK,
    settings: Settings,
    repository: Path,
    framework: TestFramework | None,
    manager: PackageManager | None,
) -> None:
    result = await runner_for(sdk, settings).run(
        make_request(repository, framework=framework, manager=manager)
    )

    assert result.infrastructure_status is InfrastructureStatus.INFRASTRUCTURE_ERROR
    assert result.test_status is TestStatus.NOT_RUN
    assert result.container_created is False
    assert sdk.create_kwargs == {}, "no container should have been created"


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


async def test_cleanup_after_success(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    sdk.on("vitest", FakeExecResult(0, report=PASSING_REPORT))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.cleanup.status is CleanupStatus.COMPLETED
    assert result.cleanup.container_removed is True
    assert result.cleanup.workspace_removed is True
    assert sdk.container is not None and sdk.container.removed is True
    assert sdk.container.remove_kwargs == {"force": True, "v": True}
    assert not (workspace_root / "exec-run-0001").exists()


async def test_cleanup_after_install_failure(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    sdk.on("install", FakeExecResult(1, b"", b"boom"))

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.cleanup.status is CleanupStatus.COMPLETED
    assert not (workspace_root / "exec-run-0001").exists()
    assert sdk.container is not None and sdk.container.removed is True


async def test_cleanup_after_timeout(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    limits = SandboxLimits.from_settings(settings).model_copy(
        update={"test_timeout_seconds": 1}
    )
    sdk.on("vitest", FakeExecResult(0, delay=3.0))

    result = await runner_for(sdk, settings).run(
        make_request(repository, limits=limits)
    )

    assert result.cleanup.status is CleanupStatus.COMPLETED
    assert not (workspace_root / "exec-run-0001").exists()


async def test_cleanup_reports_a_container_it_could_not_remove(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    sdk.remove_error = RuntimeError("device or resource busy")

    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.cleanup.status is CleanupStatus.PARTIAL
    assert result.cleanup.container_removed is False
    assert result.cleanup.workspace_removed is True
    assert result.cleanup.details
    # The workspace still goes, even when the container will not.
    assert not (workspace_root / "exec-run-0001").exists()


async def test_cleanup_refuses_a_workspace_outside_the_root(
    sdk: FakeDockerSDK, settings: Settings, tmp_path: Path
) -> None:
    runner = runner_for(sdk, settings)
    outside = tmp_path / "not-a-workspace"
    outside.mkdir()

    removed, error = runner._remove_workspace(outside)

    assert removed is False
    assert error is not None and "outside" in error
    assert outside.exists()


# --------------------------------------------------------------------------
# Security controls
# --------------------------------------------------------------------------


async def test_resource_limits_are_applied_to_the_container(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    limits = SandboxLimits(
        cpu_limit=1.5,
        memory_limit_mb=512,
        pids_limit=64,
        install_timeout_seconds=30,
        test_timeout_seconds=20,
    )

    await runner_for(sdk, settings).run(make_request(repository, limits=limits))

    created = sdk.create_kwargs
    assert created["nano_cpus"] == 1_500_000_000
    assert created["mem_limit"] == "512m"
    assert created["pids_limit"] == 64
    assert created["privileged"] is False
    assert created["cap_drop"] == ["ALL"]
    assert created["security_opt"] == ["no-new-privileges:true"]
    assert created["tmpfs"] == {"/tmp": "size=256m"}
    assert created["user"] and created["user"] != "0:0"


async def test_only_the_workspace_is_mounted_and_never_the_docker_socket(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, workspace_root: Path
) -> None:
    await runner_for(sdk, settings).run(make_request(repository))

    volumes = sdk.create_kwargs["volumes"]
    assert list(volumes) == [str((workspace_root / "exec-run-0001").resolve())]
    assert volumes[str((workspace_root / "exec-run-0001").resolve())] == {
        "bind": "/workspace",
        "mode": "rw",
    }
    assert not any("docker.sock" in str(path) for path in volumes)
    assert str(repository) not in volumes, "the clone itself is never mounted"


async def test_network_is_disabled_before_the_test_phase(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    result = await runner_for(sdk, settings).run(make_request(repository))

    assert sdk.disconnected == ["bridge"]
    assert result.network_disabled_for_test is True

    # The disconnect must land between install and test, not before install.
    commands = [call["cmd"] for call in sdk.exec_calls]
    assert commands[0][:2] == ["npm", "install"]
    assert "vitest" in commands[-1]


async def test_network_is_kept_when_configured(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    limits = SandboxLimits.from_settings(settings).model_copy(
        update={"network_enabled_during_test": True}
    )

    result = await runner_for(sdk, settings).run(
        make_request(repository, limits=limits)
    )

    assert sdk.disconnected == []
    assert result.network_disabled_for_test is False


def test_run_as_user_is_never_root() -> None:
    limits = SandboxLimits(
        cpu_limit=1,
        memory_limit_mb=256,
        pids_limit=32,
        install_timeout_seconds=10,
        test_timeout_seconds=10,
    )

    assert limits.resolve_run_as_user() != "0:0"
    assert not limits.resolve_run_as_user().startswith("0:")


# --------------------------------------------------------------------------
# Path validation and injection resistance
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("/etc/cron.d/payload.test.ts", id="absolute"),
        pytest.param("../outside.test.ts", id="parent-traversal"),
        pytest.param("src/../../escape.test.ts", id="embedded-traversal"),
    ],
)
async def test_unsafe_test_paths_are_refused(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, filename: str
) -> None:
    result = await runner_for(sdk, settings).run(
        make_request(repository, filename=filename)
    )

    assert result.infrastructure_status is InfrastructureStatus.WORKSPACE_FAILED
    assert result.container_created is False


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("../escape", id="parent-traversal"),
        pytest.param("nested/path", id="separator"),
        pytest.param("", id="empty"),
        pytest.param("run;rm -rf /", id="shell-metacharacters"),
    ],
)
async def test_unsafe_run_identifiers_are_refused(
    sdk: FakeDockerSDK, settings: Settings, repository: Path, run_id: str
) -> None:
    if run_id == "":
        # An empty identifier never reaches the runner: the request model
        # rejects it first.
        with pytest.raises(PydanticValidationError):
            make_request(repository, run_id=run_id)
        return

    result = await runner_for(sdk, settings).run(
        make_request(repository, run_id=run_id)
    )

    assert result.infrastructure_status is InfrastructureStatus.WORKSPACE_FAILED


async def test_commands_are_argument_vectors_never_shell_strings(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    await runner_for(sdk, settings).run(make_request(repository))

    assert sdk.exec_calls
    for call in sdk.exec_calls:
        assert isinstance(call["cmd"], list)
        assert all(isinstance(part, str) for part in call["cmd"])


async def test_a_filename_with_shell_metacharacters_stays_one_argument(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    hostile = "src/a; rm -rf / #.repro.test.ts"

    result = await runner_for(sdk, settings).run(
        make_request(repository, filename=hostile)
    )

    # It is a legal (if bizarre) relative path, so it is written and run — but
    # only ever as a single argv element, where no shell can split it.
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert hostile in result.test_command
    assert result.test_command.count(hostile) == 1
    assert not any("rm -rf" in part for part in result.test_command if part != hostile)


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------


async def test_vitest_runs_only_the_generated_file(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    result = await runner_for(sdk, settings).run(make_request(repository))

    assert result.test_command == (
        "npx",
        "--no-install",
        "vitest",
        "run",
        "--reporter=json",
        "--outputFile=.reprogate-report.json",
        "src/parser.repro.test.ts",
    )


async def test_jest_runs_only_the_generated_file(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    result = await runner_for(sdk, settings).run(
        make_request(repository, framework=TestFramework.JEST)
    )

    assert result.test_command == (
        "npx",
        "--no-install",
        "jest",
        "--ci",
        "--runTestsByPath",
        "src/parser.repro.test.ts",
        "--json",
        "--outputFile=.reprogate-report.json",
    )


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        pytest.param(PackageManager.NPM, ("npm", "install"), id="npm"),
        pytest.param(PackageManager.PNPM, ("pnpm", "install"), id="pnpm"),
        pytest.param(PackageManager.YARN, ("yarn", "install"), id="yarn"),
    ],
)
async def test_install_uses_the_detected_package_manager(
    sdk: FakeDockerSDK,
    settings: Settings,
    repository: Path,
    manager: PackageManager,
    expected: tuple[str, ...],
) -> None:
    result = await runner_for(sdk, settings).run(
        make_request(repository, manager=manager)
    )

    assert result.install_command[: len(expected)] == expected


async def test_corepack_is_enabled_for_pnpm(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    await runner_for(sdk, settings).run(
        make_request(repository, manager=PackageManager.PNPM)
    )

    assert sdk.exec_calls[0]["cmd"] == ["corepack", "enable"]


async def test_image_is_pulled_when_absent(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    sdk.local_images.clear()

    await runner_for(sdk, settings).run(make_request(repository))

    assert sdk.pulled == ["node:20-bookworm-slim"]


async def test_container_has_a_dead_man_timer(
    sdk: FakeDockerSDK, settings: Settings, repository: Path
) -> None:
    await runner_for(sdk, settings).run(make_request(repository))

    command = sdk.create_kwargs["command"]
    assert command[0] == "sleep"
    assert int(command[1]) > settings.sandbox_timeout_seconds
