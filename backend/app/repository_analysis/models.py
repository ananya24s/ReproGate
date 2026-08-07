"""Internal data structures produced by repository analysis.

Every value here is the result of deterministic file inspection: a file exists
or it does not, a manifest key is present or it is not. Nothing in this module
or its producers infers intent.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Language(str, Enum):
    """A programming language recognised by file extension."""

    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    KOTLIN = "kotlin"
    RUBY = "ruby"
    PHP = "php"
    CSHARP = "csharp"
    CPP = "cpp"
    C = "c"
    SWIFT = "swift"
    SCALA = "scala"
    SHELL = "shell"


class PackageManager(str, Enum):
    """A Node.js package manager."""

    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"
    BUN = "bun"


class PackageManagerSource(str, Enum):
    """What determined the package manager, in precedence order."""

    LOCKFILE = "lockfile"
    PACKAGE_MANAGER_FIELD = "package_manager_field"
    DEFAULT = "default"


class TestFramework(str, Enum):
    """A JavaScript test runner."""

    # Dunder names are not turned into enum members, so this opts the class out
    # of pytest collection without affecting the enum itself.
    __test__ = False

    JEST = "jest"
    VITEST = "vitest"


class TestFrameworkSource(str, Enum):
    """What determined the test framework, in precedence order."""

    __test__ = False

    CONFIG_FILE = "config_file"
    PACKAGE_JSON_KEY = "package_json_key"
    DEPENDENCY = "dependency"
    TEST_SCRIPT = "test_script"


class MonorepoTool(str, Enum):
    """A monorepo task runner or workspace manager found at the root."""

    PNPM_WORKSPACES = "pnpm_workspaces"
    NPM_WORKSPACES = "npm_workspaces"
    YARN_WORKSPACES = "yarn_workspaces"
    LERNA = "lerna"
    NX = "nx"
    TURBOREPO = "turborepo"


class IndexedFile(BaseModel):
    """One file discovered beneath the repository root."""

    model_config = ConfigDict(frozen=True)

    path: str
    """POSIX path relative to the repository root."""

    name: str
    suffix: str
    size_bytes: int = Field(ge=0)

    @property
    def directory(self) -> str:
        """POSIX path of the containing directory, or ``"."`` at the root."""
        head, separator, _ = self.path.rpartition("/")
        return head if separator else "."


class FileIndex(BaseModel):
    """A searchable listing of the files in a cloned repository."""

    model_config = ConfigDict(frozen=True)

    root: Path
    files: tuple[IndexedFile, ...] = ()
    paths: frozenset[str] = frozenset()
    directories: frozenset[str] = frozenset()
    truncated: bool = False
    """Whether the walk stopped at ``ANALYSIS_MAX_FILES``."""

    def contains(self, relative_path: str) -> bool:
        """Whether a file exists at ``relative_path``."""
        return relative_path in self.paths

    def has_directory(self, relative_path: str) -> bool:
        """Whether a directory exists at ``relative_path``."""
        return relative_path in self.directories

    def first_present(self, *relative_paths: str) -> str | None:
        """The first of ``relative_paths`` that exists, preserving order."""
        for candidate in relative_paths:
            if candidate in self.paths:
                return candidate
        return None

    def with_name(self, filename: str) -> tuple[IndexedFile, ...]:
        """Every file called ``filename``, at any depth."""
        return tuple(file for file in self.files if file.name == filename)


class LanguageUsage(BaseModel):
    """How much of the repository is written in one language."""

    model_config = ConfigDict(frozen=True)

    language: Language
    file_count: int = Field(ge=0)


class Lockfile(BaseModel):
    """A dependency lockfile and the manager that produces it."""

    model_config = ConfigDict(frozen=True)

    path: str
    filename: str
    package_manager: PackageManager


class NodePackage(BaseModel):
    """A parsed ``package.json``."""

    model_config = ConfigDict(frozen=True)

    path: str
    """POSIX path of the manifest, relative to the repository root."""

    directory: str
    """POSIX path of the package directory, ``"."`` at the repository root."""

    name: str | None = None
    version: str | None = None
    is_private: bool = False
    package_manager_field: str | None = None
    """The raw corepack ``packageManager`` value, if declared."""

    workspace_globs: tuple[str, ...] = ()
    scripts: dict[str, str] = Field(default_factory=dict)
    dependencies: frozenset[str] = frozenset()
    """Names from ``dependencies`` and ``devDependencies`` combined."""

    has_jest_config_key: bool = False


class RepositoryAnalysis(BaseModel):
    """The deterministic profile of a cloned repository."""

    model_config = ConfigDict(frozen=True)

    root: Path

    # -- Languages ---------------------------------------------------------
    languages: tuple[LanguageUsage, ...] = ()
    """Ranked by file count descending, then language name ascending."""

    primary_language: Language | None = None
    uses_typescript: bool = False
    typescript_config_path: str | None = None

    # -- Node.js project ---------------------------------------------------
    is_node_project: bool = False
    root_package: NodePackage | None = None
    package_manager: PackageManager | None = None
    package_manager_source: PackageManagerSource | None = None
    lockfiles: tuple[Lockfile, ...] = ()

    # -- Workspaces --------------------------------------------------------
    workspace_root: str | None = None
    is_monorepo: bool = False
    monorepo_tools: tuple[MonorepoTool, ...] = ()
    workspace_packages: tuple[NodePackage, ...] = ()

    # -- Testing -----------------------------------------------------------
    test_framework: TestFramework | None = None
    test_framework_source: TestFrameworkSource | None = None
    test_framework_config_path: str | None = None
    test_script_name: str | None = None
    test_script: str | None = None
    test_command: str | None = None

    # -- Layout ------------------------------------------------------------
    source_directories: tuple[str, ...] = ()
    test_directories: tuple[str, ...] = ()

    # -- Provenance --------------------------------------------------------
    file_count: int = Field(default=0, ge=0)
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    analyzed_at: datetime
    duration_ms: int = Field(default=0, ge=0)
