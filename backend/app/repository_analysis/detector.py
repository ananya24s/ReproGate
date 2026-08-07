"""Detects the repository language, package manager, and test framework.

Every rule here is a file-existence check, an extension count, or a manifest
key lookup. There is no inference, no scoring, and no content interpretation:
the same repository always produces the same analysis.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import Hashable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar

import yaml

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repository_analysis.file_indexer import FileIndexer
from app.repository_analysis.models import (
    FileIndex,
    Language,
    LanguageUsage,
    Lockfile,
    MonorepoTool,
    NodePackage,
    PackageManager,
    PackageManagerSource,
    RepositoryAnalysis,
    TestFramework,
    TestFrameworkSource,
)

logger = get_logger(__name__)

_HashableT = TypeVar("_HashableT", bound=Hashable)

PACKAGE_JSON: Final = "package.json"

#: File extension to language. Anything absent is simply not counted.
_EXTENSION_LANGUAGES: Final[dict[str, Language]] = {
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".rb": Language.RUBY,
    ".php": Language.PHP,
    ".cs": Language.CSHARP,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".cxx": Language.CPP,
    ".hpp": Language.CPP,
    ".c": Language.C,
    ".h": Language.C,
    ".swift": Language.SWIFT,
    ".scala": Language.SCALA,
    ".sh": Language.SHELL,
    ".bash": Language.SHELL,
}

#: Lockfile name to the manager that writes it, in precedence order.
_LOCKFILES: Final[tuple[tuple[str, PackageManager], ...]] = (
    ("pnpm-lock.yaml", PackageManager.PNPM),
    ("bun.lockb", PackageManager.BUN),
    ("bun.lock", PackageManager.BUN),
    ("yarn.lock", PackageManager.YARN),
    ("package-lock.json", PackageManager.NPM),
    ("npm-shrinkwrap.json", PackageManager.NPM),
)

_TSCONFIG_NAMES: Final = ("tsconfig.json", "tsconfig.base.json")

#: Test framework config files, checked in this order.
_FRAMEWORK_CONFIGS: Final[tuple[tuple[str, TestFramework], ...]] = tuple(
    (f"{stem}.config{extension}", framework)
    for stem, framework in (
        ("vitest", TestFramework.VITEST),
        ("jest", TestFramework.JEST),
    )
    for extension in (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".json")
)

#: Script names consulted for a test command, in order.
_TEST_SCRIPT_NAMES: Final = ("test", "test:unit", "test:ci", "tests")

#: Directory names treated as source roots when they contain source files.
_SOURCE_DIRECTORY_NAMES: Final = frozenset({"src", "lib", "app", "source", "sources"})

#: Directory names treated as test roots regardless of contents.
_TEST_DIRECTORY_NAMES: Final = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress"}
)

#: Filenames that mark the directory containing them as a test directory.
_TEST_FILE_RE: Final = re.compile(
    r"(?:\.(?:test|spec)\.(?:[cm]?[jt]sx?)$)|(?:^test_.+\.py$)|(?:_test\.py$)"
)

#: How deep a candidate source directory may sit; covers `packages/x/src`.
_MAX_SOURCE_DIRECTORY_DEPTH: Final = 3

#: `pnpm run test` and friends. Yarn resolves local binaries without `exec`.
_RUN_TEMPLATES: Final[dict[PackageManager, str]] = {
    PackageManager.NPM: "npm run {script}",
    PackageManager.YARN: "yarn run {script}",
    PackageManager.PNPM: "pnpm run {script}",
    PackageManager.BUN: "bun run {script}",
}

_EXEC_TEMPLATES: Final[dict[PackageManager, str]] = {
    PackageManager.NPM: "npx {command}",
    PackageManager.YARN: "yarn {command}",
    PackageManager.PNPM: "pnpm exec {command}",
    PackageManager.BUN: "bunx {command}",
}

#: Default non-watch invocation per framework.
_FRAMEWORK_COMMANDS: Final[dict[TestFramework, str]] = {
    TestFramework.VITEST: "vitest run",
    TestFramework.JEST: "jest",
}

_MAX_WORKSPACE_PACKAGES: Final = 500


class RepositoryDetector:
    """Derives a :class:`RepositoryAnalysis` from a cloned repository."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        indexer: FileIndexer | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._indexer = indexer or FileIndexer(self._settings)
        self._max_manifest_bytes = self._settings.analysis_max_manifest_bytes

    async def analyze(self, root: Path) -> RepositoryAnalysis:
        """Analyze the repository checked out at ``root``.

        Raises:
            RepositoryAnalysisError: ``root`` is not a readable directory.
        """
        index = await self._indexer.index(root)
        return await asyncio.to_thread(self.analyze_index, index)

    def analyze_index(self, index: FileIndex) -> RepositoryAnalysis:
        """Analyze an already-built file index."""
        started = time.monotonic()
        warnings: list[str] = []
        root = index.root

        languages = _detect_languages(index)
        primary_language = languages[0].language if languages else None

        typescript_config_path = index.first_present(*_TSCONFIG_NAMES)
        uses_typescript = typescript_config_path is not None or any(
            usage.language is Language.TYPESCRIPT for usage in languages
        )

        root_package = self._load_package(root, PACKAGE_JSON, warnings)
        is_node_project = root_package is not None or index.contains(PACKAGE_JSON)

        lockfiles = _detect_lockfiles(index)
        package_manager, package_manager_source = _detect_package_manager(
            lockfiles, root_package, is_node_project
        )

        workspace_globs, monorepo_tools = self._detect_workspaces(
            root, index, root_package, warnings
        )
        workspace_packages = self._load_workspace_packages(
            root, workspace_globs, warnings
        )

        framework, framework_source, framework_config = _detect_test_framework(
            index, root_package
        )
        script_name, script_body = _detect_test_script(root_package)
        test_command = _build_test_command(package_manager, script_name, framework)

        analysis = RepositoryAnalysis(
            root=root,
            languages=languages,
            primary_language=primary_language,
            uses_typescript=uses_typescript,
            typescript_config_path=typescript_config_path,
            is_node_project=is_node_project,
            root_package=root_package,
            package_manager=package_manager,
            package_manager_source=package_manager_source,
            lockfiles=lockfiles,
            workspace_root="." if root_package is not None else None,
            is_monorepo=bool(workspace_globs),
            monorepo_tools=monorepo_tools,
            workspace_packages=workspace_packages,
            test_framework=framework,
            test_framework_source=framework_source,
            test_framework_config_path=framework_config,
            test_script_name=script_name,
            test_script=script_body,
            test_command=test_command,
            source_directories=_detect_source_directories(index),
            test_directories=_detect_test_directories(index),
            file_count=len(index.files),
            truncated=index.truncated,
            warnings=tuple(warnings),
            analyzed_at=datetime.now(tz=UTC),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        logger.info(
            "Analyzed repository",
            extra={
                "root": str(root),
                "primary_language": primary_language.value
                if primary_language
                else None,
                "is_node_project": analysis.is_node_project,
                "package_manager": (package_manager.value if package_manager else None),
                "is_monorepo": analysis.is_monorepo,
                "workspace_packages": len(workspace_packages),
                "test_framework": framework.value if framework else None,
                "test_command": test_command,
                "file_count": analysis.file_count,
                "warning_count": len(warnings),
            },
        )
        return analysis

    def _read_manifest(self, root: Path, relative_path: str) -> str | None:
        """Read a manifest, refusing anything outside the root or oversized."""
        candidate = root / relative_path
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root.resolve()):
                return None
            if not resolved.is_file():
                return None
            if resolved.stat().st_size > self._max_manifest_bytes:
                return None
            return resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _load_package(
        self, root: Path, relative_path: str, warnings: list[str]
    ) -> NodePackage | None:
        """Parse a ``package.json`` into a :class:`NodePackage`."""
        raw = self._read_manifest(root, relative_path)
        if raw is None:
            return None

        try:
            payload = json.loads(raw)
        except ValueError:
            warnings.append(f"{relative_path} is not valid JSON and was skipped.")
            return None

        if not isinstance(payload, dict):
            warnings.append(f"{relative_path} is not a JSON object and was skipped.")
            return None

        directory = relative_path.rpartition("/")[0] or "."
        return NodePackage(
            path=relative_path,
            directory=directory,
            name=_optional_str(payload.get("name")),
            version=_optional_str(payload.get("version")),
            is_private=payload.get("private") is True,
            package_manager_field=_optional_str(payload.get("packageManager")),
            workspace_globs=_workspace_globs(payload.get("workspaces")),
            scripts=_string_mapping(payload.get("scripts")),
            dependencies=frozenset(
                _string_mapping(payload.get("dependencies"))
                | _string_mapping(payload.get("devDependencies"))
            ),
            has_jest_config_key=isinstance(payload.get("jest"), dict),
        )

    def _detect_workspaces(
        self,
        root: Path,
        index: FileIndex,
        root_package: NodePackage | None,
        warnings: list[str],
    ) -> tuple[tuple[str, ...], tuple[MonorepoTool, ...]]:
        """Collect declared workspace globs and the tools that declare them."""
        globs: list[str] = []
        tools: list[MonorepoTool] = []

        if root_package is not None and root_package.workspace_globs:
            globs.extend(root_package.workspace_globs)
            # npm and yarn share the `workspaces` field; the lockfile decides.
            tools.append(
                MonorepoTool.YARN_WORKSPACES
                if index.contains("yarn.lock")
                else MonorepoTool.NPM_WORKSPACES
            )

        if index.contains("pnpm-workspace.yaml"):
            raw = self._read_manifest(root, "pnpm-workspace.yaml")
            parsed = _safe_yaml(raw)
            if parsed is None and raw is not None:
                warnings.append(
                    "pnpm-workspace.yaml is not valid YAML and was skipped."
                )
            elif isinstance(parsed, dict):
                globs.extend(_string_sequence(parsed.get("packages")))
            tools.append(MonorepoTool.PNPM_WORKSPACES)

        if index.contains("lerna.json"):
            raw = self._read_manifest(root, "lerna.json")
            parsed = _safe_json(raw)
            if parsed is None and raw is not None:
                warnings.append("lerna.json is not valid JSON and was skipped.")
            elif isinstance(parsed, dict):
                globs.extend(_string_sequence(parsed.get("packages")))
            tools.append(MonorepoTool.LERNA)

        if index.contains("nx.json"):
            tools.append(MonorepoTool.NX)
        if index.contains("turbo.json"):
            tools.append(MonorepoTool.TURBOREPO)

        return _unique(globs), _unique(tools)

    def _load_workspace_packages(
        self, root: Path, globs: tuple[str, ...], warnings: list[str]
    ) -> tuple[NodePackage, ...]:
        """Expand workspace globs and parse each package manifest found."""
        manifests: list[str] = []
        seen: set[str] = set()

        for pattern in globs:
            # A glob that climbs out of the repository is never legitimate.
            if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                warnings.append(f"Workspace pattern {pattern!r} was ignored.")
                continue

            try:
                matches = sorted(root.glob(f"{pattern.rstrip('/')}/{PACKAGE_JSON}"))
            except (OSError, ValueError):
                warnings.append(f"Workspace pattern {pattern!r} could not be expanded.")
                continue

            for match in matches:
                if not match.is_file() or match.is_symlink():
                    continue
                try:
                    relative = match.relative_to(root).as_posix()
                except ValueError:
                    continue
                if "node_modules" in relative.split("/") or relative in seen:
                    continue
                seen.add(relative)
                manifests.append(relative)
                if len(manifests) >= _MAX_WORKSPACE_PACKAGES:
                    warnings.append(
                        "Workspace expansion stopped at "
                        f"{_MAX_WORKSPACE_PACKAGES} packages."
                    )
                    break
            if len(manifests) >= _MAX_WORKSPACE_PACKAGES:
                break

        packages = [
            package
            for relative in sorted(manifests)
            if (package := self._load_package(root, relative, warnings)) is not None
        ]
        return tuple(packages)


# --------------------------------------------------------------------------
# Detection rules
# --------------------------------------------------------------------------


def _detect_languages(index: FileIndex) -> tuple[LanguageUsage, ...]:
    """Count files per language, ranked by count then name for stability."""
    counts: Counter[Language] = Counter()
    for file in index.files:
        language = _EXTENSION_LANGUAGES.get(file.suffix)
        if language is not None:
            counts[language] += 1

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].value))
    return tuple(
        LanguageUsage(language=language, file_count=count)
        for language, count in ordered
    )


def _detect_lockfiles(index: FileIndex) -> tuple[Lockfile, ...]:
    """Find root-level lockfiles in manager precedence order."""
    return tuple(
        Lockfile(path=filename, filename=filename, package_manager=manager)
        for filename, manager in _LOCKFILES
        if index.contains(filename)
    )


def _detect_package_manager(
    lockfiles: tuple[Lockfile, ...],
    root_package: NodePackage | None,
    is_node_project: bool,
) -> tuple[PackageManager | None, PackageManagerSource | None]:
    """Choose a package manager.

    A committed lockfile wins: it is what ``--frozen-lockfile`` installs need.
    A corepack ``packageManager`` field is the next best declaration, and npm
    is the fallback for any Node project without either.
    """
    if lockfiles:
        return lockfiles[0].package_manager, PackageManagerSource.LOCKFILE

    if root_package is not None and root_package.package_manager_field:
        declared = root_package.package_manager_field.split("@", 1)[0].strip().lower()
        for manager in PackageManager:
            if manager.value == declared:
                return manager, PackageManagerSource.PACKAGE_MANAGER_FIELD

    if is_node_project:
        return PackageManager.NPM, PackageManagerSource.DEFAULT
    return None, None


def _detect_test_framework(
    index: FileIndex, root_package: NodePackage | None
) -> tuple[TestFramework | None, TestFrameworkSource | None, str | None]:
    """Identify the test runner from config, manifest keys, then scripts."""
    for filename, framework in _FRAMEWORK_CONFIGS:
        if index.contains(filename):
            return framework, TestFrameworkSource.CONFIG_FILE, filename

    if root_package is None:
        return None, None, None

    if root_package.has_jest_config_key:
        return TestFramework.JEST, TestFrameworkSource.PACKAGE_JSON_KEY, PACKAGE_JSON

    for framework in (TestFramework.VITEST, TestFramework.JEST):
        if framework.value in root_package.dependencies:
            return framework, TestFrameworkSource.DEPENDENCY, None

    for script in root_package.scripts.values():
        words = re.findall(r"[A-Za-z0-9_-]+", script)
        for framework in (TestFramework.VITEST, TestFramework.JEST):
            if framework.value in words:
                return framework, TestFrameworkSource.TEST_SCRIPT, None

    return None, None, None


def _detect_test_script(
    root_package: NodePackage | None,
) -> tuple[str | None, str | None]:
    """Pick the script that runs the test suite."""
    if root_package is None:
        return None, None

    for name in _TEST_SCRIPT_NAMES:
        body = root_package.scripts.get(name)
        if body:
            return name, body
    return None, None


def _build_test_command(
    package_manager: PackageManager | None,
    script_name: str | None,
    framework: TestFramework | None,
) -> str | None:
    """Compose the command a sandbox would run, without executing anything."""
    if package_manager is None:
        return None

    if script_name is not None:
        return _RUN_TEMPLATES[package_manager].format(script=script_name)

    if framework is not None:
        return _EXEC_TEMPLATES[package_manager].format(
            command=_FRAMEWORK_COMMANDS[framework]
        )
    return None


def _detect_source_directories(index: FileIndex) -> tuple[str, ...]:
    """Directories named like source roots that actually contain source."""
    directories_with_source = {
        file.directory for file in index.files if file.suffix in _EXTENSION_LANGUAGES
    }

    matches = {
        directory
        for directory in index.directories
        if directory.rpartition("/")[2] in _SOURCE_DIRECTORY_NAMES
        and directory.count("/") < _MAX_SOURCE_DIRECTORY_DEPTH
        and any(
            candidate == directory or candidate.startswith(f"{directory}/")
            for candidate in directories_with_source
        )
    }

    if not matches and "." in directories_with_source:
        # A flat repository with sources sitting at the root.
        return (".",)
    return tuple(sorted(matches))


def _detect_test_directories(index: FileIndex) -> tuple[str, ...]:
    """Directories named like test roots, or holding recognisable test files."""
    matches = {
        directory
        for directory in index.directories
        if directory.rpartition("/")[2] in _TEST_DIRECTORY_NAMES
    }
    matches |= {
        file.directory
        for file in index.files
        if file.directory != "." and _TEST_FILE_RE.search(file.name)
    }
    return tuple(sorted(_prune_nested(matches)))


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _prune_nested(directories: set[str]) -> set[str]:
    """Drop directories that sit beneath another directory in the set."""
    return {
        directory
        for directory in directories
        if not any(
            directory.startswith(f"{other}/")
            for other in directories
            if other != directory
        )
    }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _workspace_globs(value: object) -> tuple[str, ...]:
    """Read npm/yarn ``workspaces``, which is a list or ``{"packages": [...]}``."""
    if isinstance(value, dict):
        return _string_sequence(value.get("packages"))
    return _string_sequence(value)


def _safe_json(raw: str | None) -> Any | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _safe_yaml(raw: str | None) -> Any | None:
    if raw is None:
        return None
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return None


def _unique(values: list[_HashableT]) -> tuple[_HashableT, ...]:
    """Deduplicate while preserving first-seen order."""
    seen: set[_HashableT] = set()
    ordered: list[_HashableT] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)
