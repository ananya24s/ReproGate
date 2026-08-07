"""Unit tests for deterministic repository analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import RepositoryAnalysisError
from app.repository_analysis import (
    FileIndexer,
    Language,
    MonorepoTool,
    PackageManager,
    PackageManagerSource,
    RepositoryAnalysis,
    RepositoryDetector,
    TestFramework,
    TestFrameworkSource,
)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def detector(settings: Settings) -> RepositoryDetector:
    return RepositoryDetector(settings)


def write(path: Path, content: str = "") -> Path:
    """Create a file and every directory above it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def write_package_json(directory: Path, **fields: Any) -> Path:
    return write(directory / "package.json", json.dumps(fields))


@pytest.fixture
def node_repo(tmp_path: Path) -> Path:
    """A single-package TypeScript project using pnpm and vitest."""
    repo = tmp_path / "repo"
    write_package_json(
        repo,
        name="widget",
        version="1.0.0",
        private=True,
        scripts={"build": "tsc", "test": "vitest run"},
        devDependencies={"vitest": "^2.0.0", "typescript": "^5.6.0"},
    )
    write(repo / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    write(repo / "tsconfig.json", "{}")
    write(repo / "vitest.config.ts", "export default {}")
    write(repo / "src" / "index.ts", "export const a = 1;\n")
    write(repo / "src" / "helper.ts", "export const b = 2;\n")
    write(repo / "tests" / "index.test.ts", "test('x', () => {});\n")
    return repo


# --------------------------------------------------------------------------
# File indexing
# --------------------------------------------------------------------------


async def test_indexer_skips_dependency_and_build_directories(
    settings: Settings, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "src" / "index.ts")
    write(repo / "node_modules" / "left-pad" / "index.js")
    write(repo / "dist" / "bundle.js")
    write(repo / ".git" / "config")

    index = await FileIndexer(settings).index(repo)

    assert index.contains("src/index.ts")
    assert not any("node_modules" in file.path for file in index.files)
    assert not any(file.path.startswith("dist/") for file in index.files)
    assert not any(file.path.startswith(".git/") for file in index.files)


async def test_indexer_does_not_follow_symlinks(
    settings: Settings, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    write(outside / "secret.ts", "export const secret = 1;\n")
    repo = tmp_path / "repo"
    write(repo / "src" / "index.ts")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    (repo / "linked.ts").symlink_to(outside / "secret.ts")

    index = await FileIndexer(settings).index(repo)

    assert not any("linked" in file.path for file in index.files)


async def test_indexer_honours_the_file_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for number in range(20):
        write(repo / f"file{number:02d}.ts")

    index = await FileIndexer(Settings(analysis_max_files=5)).index(repo)

    assert index.truncated is True
    assert len(index.files) == 5


async def test_indexer_rejects_a_missing_directory(
    settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(RepositoryAnalysisError):
        await FileIndexer(settings).index(tmp_path / "nope")


# --------------------------------------------------------------------------
# Languages, TypeScript vs JavaScript
# --------------------------------------------------------------------------


async def test_detects_languages_ranked_by_file_count(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "a.ts")
    write(repo / "b.ts")
    write(repo / "c.ts")
    write(repo / "d.py")
    write(repo / "e.py")
    write(repo / "f.go")

    analysis = await detector.analyze(repo)

    assert [(usage.language, usage.file_count) for usage in analysis.languages] == [
        (Language.TYPESCRIPT, 3),
        (Language.PYTHON, 2),
        (Language.GO, 1),
    ]
    assert analysis.primary_language is Language.TYPESCRIPT


async def test_typescript_detected_from_config_without_sources(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "tsconfig.json", "{}")
    write(repo / "index.js")

    analysis = await detector.analyze(repo)

    assert analysis.uses_typescript is True
    assert analysis.typescript_config_path == "tsconfig.json"
    assert analysis.primary_language is Language.JAVASCRIPT


async def test_javascript_project_is_not_typescript(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="plain")
    write(repo / "src" / "index.js")

    analysis = await detector.analyze(repo)

    assert analysis.uses_typescript is False
    assert analysis.typescript_config_path is None


# --------------------------------------------------------------------------
# Node project, package manager, lockfiles
# --------------------------------------------------------------------------


async def test_detects_node_project_and_manifest(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    analysis = await detector.analyze(node_repo)

    assert analysis.is_node_project is True
    assert analysis.root_package is not None
    assert analysis.root_package.name == "widget"
    assert analysis.root_package.version == "1.0.0"
    assert analysis.root_package.is_private is True
    assert analysis.root_package.directory == "."
    assert "vitest" in analysis.root_package.dependencies
    assert analysis.workspace_root == "."


async def test_non_node_project(detector: RepositoryDetector, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write(repo / "main.py", "print('hi')\n")

    analysis = await detector.analyze(repo)

    assert analysis.is_node_project is False
    assert analysis.root_package is None
    assert analysis.package_manager is None
    assert analysis.package_manager_source is None
    assert analysis.test_command is None
    assert analysis.workspace_root is None


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", PackageManager.PNPM),
        ("yarn.lock", PackageManager.YARN),
        ("package-lock.json", PackageManager.NPM),
        ("npm-shrinkwrap.json", PackageManager.NPM),
        ("bun.lockb", PackageManager.BUN),
        ("bun.lock", PackageManager.BUN),
    ],
)
async def test_package_manager_from_lockfile(
    detector: RepositoryDetector,
    tmp_path: Path,
    lockfile: str,
    expected: PackageManager,
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x")
    write(repo / lockfile)

    analysis = await detector.analyze(repo)

    assert analysis.package_manager is expected
    assert analysis.package_manager_source is PackageManagerSource.LOCKFILE
    assert [entry.filename for entry in analysis.lockfiles] == [lockfile]


async def test_lockfile_precedence_when_several_are_committed(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x")
    write(repo / "package-lock.json")
    write(repo / "yarn.lock")
    write(repo / "pnpm-lock.yaml")

    analysis = await detector.analyze(repo)

    assert analysis.package_manager is PackageManager.PNPM
    assert [entry.filename for entry in analysis.lockfiles] == [
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
    ]


async def test_package_manager_from_corepack_field(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", packageManager="yarn@4.5.0")

    analysis = await detector.analyze(repo)

    assert analysis.package_manager is PackageManager.YARN
    assert analysis.package_manager_source is PackageManagerSource.PACKAGE_MANAGER_FIELD


async def test_package_manager_defaults_to_npm(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x")

    analysis = await detector.analyze(repo)

    assert analysis.package_manager is PackageManager.NPM
    assert analysis.package_manager_source is PackageManagerSource.DEFAULT


async def test_lockfile_beats_the_corepack_field(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", packageManager="yarn@4.5.0")
    write(repo / "pnpm-lock.yaml")

    analysis = await detector.analyze(repo)

    assert analysis.package_manager is PackageManager.PNPM
    assert analysis.package_manager_source is PackageManagerSource.LOCKFILE


# --------------------------------------------------------------------------
# Monorepo and workspaces
# --------------------------------------------------------------------------


async def test_npm_workspaces_monorepo(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root", workspaces=["packages/*"])
    write(repo / "package-lock.json")
    write_package_json(repo / "packages" / "alpha", name="@scope/alpha")
    write_package_json(repo / "packages" / "beta", name="@scope/beta")

    analysis = await detector.analyze(repo)

    assert analysis.is_monorepo is True
    assert analysis.monorepo_tools == (MonorepoTool.NPM_WORKSPACES,)
    assert [package.name for package in analysis.workspace_packages] == [
        "@scope/alpha",
        "@scope/beta",
    ]
    assert [package.directory for package in analysis.workspace_packages] == [
        "packages/alpha",
        "packages/beta",
    ]


async def test_yarn_workspaces_are_named_from_the_lockfile(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root", workspaces={"packages": ["apps/*"]})
    write(repo / "yarn.lock")
    write_package_json(repo / "apps" / "web", name="web")

    analysis = await detector.analyze(repo)

    assert analysis.monorepo_tools == (MonorepoTool.YARN_WORKSPACES,)
    assert [package.name for package in analysis.workspace_packages] == ["web"]


async def test_pnpm_workspace_yaml_monorepo(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root")
    write(repo / "pnpm-lock.yaml")
    write(repo / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    write_package_json(repo / "packages" / "core", name="core")

    analysis = await detector.analyze(repo)

    assert analysis.is_monorepo is True
    assert analysis.monorepo_tools == (MonorepoTool.PNPM_WORKSPACES,)
    assert [package.name for package in analysis.workspace_packages] == ["core"]


async def test_lerna_and_task_runners_are_recorded(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root")
    write(repo / "lerna.json", json.dumps({"packages": ["modules/*"]}))
    write(repo / "nx.json", "{}")
    write(repo / "turbo.json", "{}")
    write_package_json(repo / "modules" / "one", name="one")

    analysis = await detector.analyze(repo)

    assert analysis.is_monorepo is True
    assert set(analysis.monorepo_tools) == {
        MonorepoTool.LERNA,
        MonorepoTool.NX,
        MonorepoTool.TURBOREPO,
    }


async def test_single_package_repository_is_not_a_monorepo(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    analysis = await detector.analyze(node_repo)

    assert analysis.is_monorepo is False
    assert analysis.monorepo_tools == ()
    assert analysis.workspace_packages == ()


async def test_nested_package_json_without_a_declaration_is_not_a_monorepo(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root")
    write_package_json(repo / "examples" / "demo", name="demo")

    analysis = await detector.analyze(repo)

    assert analysis.is_monorepo is False
    assert analysis.workspace_packages == ()


async def test_workspace_globs_cannot_escape_the_repository(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    write_package_json(outside, name="outsider")
    repo = tmp_path / "repo"
    write_package_json(repo, name="root", workspaces=["../outside"])

    analysis = await detector.analyze(repo)

    assert analysis.workspace_packages == ()
    assert any("ignored" in warning for warning in analysis.warnings)


# --------------------------------------------------------------------------
# Test framework and command
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config_name", "expected"),
    [
        ("vitest.config.ts", TestFramework.VITEST),
        ("vitest.config.js", TestFramework.VITEST),
        ("jest.config.ts", TestFramework.JEST),
        ("jest.config.cjs", TestFramework.JEST),
        ("jest.config.json", TestFramework.JEST),
    ],
)
async def test_framework_from_config_file(
    detector: RepositoryDetector,
    tmp_path: Path,
    config_name: str,
    expected: TestFramework,
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x")
    write(repo / config_name, "{}")

    analysis = await detector.analyze(repo)

    assert analysis.test_framework is expected
    assert analysis.test_framework_source is TestFrameworkSource.CONFIG_FILE
    assert analysis.test_framework_config_path == config_name


async def test_framework_from_package_json_jest_key(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", jest={"testEnvironment": "node"})

    analysis = await detector.analyze(repo)

    assert analysis.test_framework is TestFramework.JEST
    assert analysis.test_framework_source is TestFrameworkSource.PACKAGE_JSON_KEY


async def test_framework_from_dev_dependency(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", devDependencies={"jest": "^29.0.0"})

    analysis = await detector.analyze(repo)

    assert analysis.test_framework is TestFramework.JEST
    assert analysis.test_framework_source is TestFrameworkSource.DEPENDENCY


async def test_framework_from_test_script(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", scripts={"test": "vitest run --coverage"})

    analysis = await detector.analyze(repo)

    assert analysis.test_framework is TestFramework.VITEST
    assert analysis.test_framework_source is TestFrameworkSource.TEST_SCRIPT


async def test_no_framework_detected(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", scripts={"build": "tsc"})

    analysis = await detector.analyze(repo)

    assert analysis.test_framework is None
    assert analysis.test_framework_source is None
    assert analysis.test_command is None


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", "pnpm run test"),
        ("yarn.lock", "yarn run test"),
        ("package-lock.json", "npm run test"),
        ("bun.lockb", "bun run test"),
    ],
)
async def test_test_command_uses_the_detected_package_manager(
    detector: RepositoryDetector, tmp_path: Path, lockfile: str, expected: str
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", scripts={"test": "vitest run"})
    write(repo / lockfile)

    analysis = await detector.analyze(repo)

    assert analysis.test_command == expected
    assert analysis.test_script_name == "test"
    assert analysis.test_script == "vitest run"


async def test_test_command_falls_back_to_the_framework_binary(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", devDependencies={"vitest": "^2.0.0"})
    write(repo / "pnpm-lock.yaml")

    analysis = await detector.analyze(repo)

    assert analysis.test_script_name is None
    assert analysis.test_command == "pnpm exec vitest run"


async def test_alternate_test_script_names_are_used(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="x", scripts={"test:unit": "jest --ci"})

    analysis = await detector.analyze(repo)

    assert analysis.test_script_name == "test:unit"
    assert analysis.test_command == "npm run test:unit"


# --------------------------------------------------------------------------
# Source and test directories
# --------------------------------------------------------------------------


async def test_detects_source_and_test_directories(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    analysis = await detector.analyze(node_repo)

    assert analysis.source_directories == ("src",)
    assert analysis.test_directories == ("tests",)


async def test_source_directory_must_contain_source_files(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "src" / "README.md", "# not source\n")
    write(repo / "lib" / "index.ts")

    analysis = await detector.analyze(repo)

    assert analysis.source_directories == ("lib",)


async def test_monorepo_package_source_directories(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root", workspaces=["packages/*"])
    write(repo / "packages" / "alpha" / "src" / "index.ts")
    write(repo / "packages" / "beta" / "src" / "index.ts")

    analysis = await detector.analyze(repo)

    assert analysis.source_directories == (
        "packages/alpha/src",
        "packages/beta/src",
    )


async def test_flat_repository_reports_the_root_as_source(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "index.ts")

    analysis = await detector.analyze(repo)

    assert analysis.source_directories == (".",)


async def test_test_directories_from_colocated_test_files(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "src" / "index.ts")
    write(repo / "src" / "index.test.ts")
    write(repo / "src" / "nested" / "deep.spec.tsx")

    analysis = await detector.analyze(repo)

    # `src/nested` is pruned because `src` already covers it.
    assert analysis.test_directories == ("src",)


async def test_named_test_directories_are_detected(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "__tests__" / "a.ts")
    write(repo / "e2e" / "b.ts")
    write(repo / "src" / "index.ts")

    analysis = await detector.analyze(repo)

    assert analysis.test_directories == ("__tests__", "e2e")


# --------------------------------------------------------------------------
# Robustness and determinism
# --------------------------------------------------------------------------


async def test_malformed_package_json_is_reported_not_fatal(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write(repo / "package.json", "{ this is not json")
    write(repo / "src" / "index.ts")

    analysis = await detector.analyze(repo)

    assert analysis.root_package is None
    # The file exists, so this is still a Node project with a broken manifest.
    assert analysis.is_node_project is True
    assert any("not valid JSON" in warning for warning in analysis.warnings)


async def test_malformed_pnpm_workspace_is_reported_not_fatal(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    write_package_json(repo, name="root")
    write(repo / "pnpm-workspace.yaml", "packages:\n  - [unclosed\n")

    analysis = await detector.analyze(repo)

    assert analysis.monorepo_tools == (MonorepoTool.PNPM_WORKSPACES,)
    assert any("not valid YAML" in warning for warning in analysis.warnings)


async def test_empty_repository_analyses_cleanly(
    detector: RepositoryDetector, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    analysis = await detector.analyze(repo)

    assert isinstance(analysis, RepositoryAnalysis)
    assert analysis.languages == ()
    assert analysis.primary_language is None
    assert analysis.is_node_project is False
    assert analysis.file_count == 0
    assert analysis.source_directories == ()
    assert analysis.test_directories == ()


async def test_analysis_is_deterministic(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    first = await detector.analyze(node_repo)
    second = await detector.analyze(node_repo)

    ignore = {"analyzed_at", "duration_ms"}
    assert first.model_dump(exclude=ignore) == second.model_dump(exclude=ignore)


async def test_analysis_records_provenance(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    analysis = await detector.analyze(node_repo)

    assert analysis.root == node_repo.resolve()
    assert analysis.file_count == 7
    assert analysis.truncated is False
    assert analysis.warnings == ()
    assert analysis.analyzed_at.tzinfo is not None
    assert analysis.duration_ms >= 0


async def test_full_profile_of_the_reference_project(
    detector: RepositoryDetector, node_repo: Path
) -> None:
    analysis = await detector.analyze(node_repo)

    assert analysis.primary_language is Language.TYPESCRIPT
    assert analysis.uses_typescript is True
    assert analysis.is_node_project is True
    assert analysis.package_manager is PackageManager.PNPM
    assert analysis.is_monorepo is False
    assert analysis.test_framework is TestFramework.VITEST
    assert analysis.test_command == "pnpm run test"
    assert analysis.source_directories == ("src",)
    assert analysis.test_directories == ("tests",)
