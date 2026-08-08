"""Real Docker execution of a generated reproduction test.

Separated from the unit suite in two ways: these are marked ``integration`` and
deselected by the default ``addopts``, and they skip outright when no daemon
answers. Run them with::

    pytest -m integration tests/integration

They pull a Node image and install Vitest from the public registry, so expect
the first run to take a couple of minutes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

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

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    try:
        return asyncio.run(SandboxDockerClient().ping())
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="no Docker daemon is reachable"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        sandbox_workspace_root=str(tmp_path / "workspaces"),
        sandbox_install_timeout_seconds=600,
        sandbox_timeout_seconds=180,
        sandbox_memory_limit_mb=1024,
        sandbox_cpu_limit=2.0,
    )


@pytest.fixture
def node_repository(tmp_path: Path) -> Path:
    """The smallest real Vitest project that can demonstrate both outcomes."""
    root = tmp_path / "fixture"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "reprogate-fixture",
                "private": True,
                "type": "module",
                "scripts": {"test": "vitest run"},
                "devDependencies": {"vitest": "^2.1.0"},
            }
        )
    )
    (root / "src" / "math.js").write_text(
        "export function add(a, b) {\n  return a + b;\n}\n"
    )
    return root


def _analysis() -> RepositoryAnalysis:
    return RepositoryAnalysis(
        root="/fixture",
        languages=(LanguageUsage(language=Language.JAVASCRIPT, file_count=1),),
        primary_language=Language.JAVASCRIPT,
        is_node_project=True,
        root_package=NodePackage(
            path="package.json", directory=".", name="reprogate-fixture"
        ),
        package_manager=PackageManager.NPM,
        test_framework=TestFramework.VITEST,
        test_command="npm run test",
        analyzed_at=datetime.now(tz=UTC),
    )


def _generated_test(source: str) -> GeneratedReproductionTest:
    return GeneratedReproductionTest(
        language=TestLanguage.JAVASCRIPT,
        framework=TestFramework.VITEST,
        filename="src/repro.test.js",
        source=source,
        reasoning_summary="Exercises add() as the report describes.",
        confidence=Confidence.HIGH,
        expected_outcome=ExpectedOutcome(
            signal=ExpectedSignal.FAILS_WHEN_ISSUE_PRESENT,
            description="Would fail if add() misbehaves.",
        ),
    )


PASSING_TEST = (
    "import { it, expect } from 'vitest';\n"
    "import { add } from './math.js';\n"
    "it('adds', () => { expect(add(1, 2)).toBe(3); });\n"
)

FAILING_TEST = (
    "import { it, expect } from 'vitest';\n"
    "import { add } from './math.js';\n"
    "it('reproduces the report', () => { expect(add(1, 2)).toBe(5); });\n"
)

CRASHING_TEST = (
    "import { it } from 'vitest';\n"
    "import { missing } from './does-not-exist.js';\n"
    "it('never runs', () => missing());\n"
)


@requires_docker
async def test_real_container_runs_a_passing_test(
    settings: Settings, node_repository: Path
) -> None:
    result = await SandboxRunner(settings=settings).run(
        SandboxExecutionRequest(
            run_id="integration-pass",
            repository_path=node_repository,
            repository_analysis=_analysis(),
            generated_test=_generated_test(PASSING_TEST),
        )
    )

    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.test_status is TestStatus.PASSED
    assert result.dependencies_installed is True
    assert result.report.available is True
    assert result.report.tests_passed == 1
    assert result.cleanup.status is CleanupStatus.COMPLETED
    assert not Path(settings.sandbox_workspace_root, "exec-integration-pass").exists()


@requires_docker
async def test_real_container_reports_a_failing_test_as_a_test_outcome(
    settings: Settings, node_repository: Path
) -> None:
    result = await SandboxRunner(settings=settings).run(
        SandboxExecutionRequest(
            run_id="integration-fail",
            repository_path=node_repository,
            repository_analysis=_analysis(),
            generated_test=_generated_test(FAILING_TEST),
        )
    )

    # The whole point: a failing reproduction test is a successful execution.
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.infrastructure_ok is True
    assert result.test_status is TestStatus.FAILED
    assert result.report.tests_failed == 1
    assert result.failure_reason is None


@requires_docker
async def test_real_container_reports_a_crashing_test_as_errored(
    settings: Settings, node_repository: Path
) -> None:
    result = await SandboxRunner(settings=settings).run(
        SandboxExecutionRequest(
            run_id="integration-error",
            repository_path=node_repository,
            repository_analysis=_analysis(),
            generated_test=_generated_test(CRASHING_TEST),
        )
    )

    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.test_status is TestStatus.ERRORED


@requires_docker
async def test_real_container_has_no_network_during_the_test_phase(
    settings: Settings, node_repository: Path
) -> None:
    source = (
        "import { it, expect } from 'vitest';\n"
        "it('cannot reach the network', async () => {\n"
        "  await expect(fetch('https://registry.npmjs.org/')).rejects.toThrow();\n"
        "});\n"
    )
    result = await SandboxRunner(settings=settings).run(
        SandboxExecutionRequest(
            run_id="integration-network",
            repository_path=node_repository,
            repository_analysis=_analysis(),
            generated_test=_generated_test(source),
        )
    )

    assert result.network_disabled_for_test is True
    assert result.infrastructure_status is InfrastructureStatus.COMPLETED
    assert result.test_status is TestStatus.PASSED


@requires_docker
async def test_real_container_enforces_the_test_timeout(
    settings: Settings, node_repository: Path
) -> None:
    source = (
        "import { it } from 'vitest';\n"
        "it('never finishes', async () => {\n"
        "  await new Promise(() => {});\n"
        "}, 600000);\n"
    )
    limits = SandboxLimits.from_settings(settings).model_copy(
        update={"test_timeout_seconds": 20}
    )

    result = await SandboxRunner(settings=settings).run(
        SandboxExecutionRequest(
            run_id="integration-timeout",
            repository_path=node_repository,
            repository_analysis=_analysis(),
            generated_test=_generated_test(source),
            limits=limits,
        )
    )

    assert result.infrastructure_status is InfrastructureStatus.TEST_TIMEOUT
    assert result.test_status is TestStatus.NOT_RUN
    assert result.timeout is not None
    assert result.cleanup.container_removed is True
    assert result.cleanup.workspace_removed is True
