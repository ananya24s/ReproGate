"""Reads GitHub repository data: metadata, default branch, commits, and source
code retrieval for a given revision.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConfigurationError,
    GitHubError,
    RepositoryCloneError,
    RepositoryCloneTimeoutError,
    RepositoryNotClonableError,
    WorkspaceError,
)
from app.core.logging import get_logger
from app.github.client import GitHubClient, JsonObject
from app.schemas.github import ClonedRepository, GitHubIssueRef, GitHubRepository

logger = get_logger(__name__)

# Clone URLs come from our own GitHub API responses, but the transport is
# validated anyway: git's `ext::` helper executes arbitrary commands, and
# `file://` would expose the host filesystem.
_ALLOWED_CLONE_SCHEMES: Final = frozenset({"https"})
_ALLOWED_CLONE_HOSTS: Final = frozenset({"github.com", "www.github.com"})

# Workspace directory names are derived from a run identifier; keep them to
# characters that cannot escape the workspace root.
_RUN_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# A conservative subset of git's ref rules — enough to reject anything that
# could be read as an option or traverse a path.
_BRANCH_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

#: Substrings git uses to report a repository or branch that cannot be reached.
_NOT_CLONABLE_MARKERS: Final = (
    "repository not found",
    "could not read from remote repository",
    "does not appear to be a git repository",
    "remote branch",
    "authentication failed",
    "could not read username",
    "terminal prompts disabled",
    "access denied",
    "permission denied",
)

#: How much of git's stderr to keep in logs and error messages.
_STDERR_EXCERPT_LIMIT: Final = 500


class GitHubRepositoryService:
    """Fetches repository metadata from the GitHub REST API."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def get_repository(self, owner: str, name: str) -> GitHubRepository:
        """Fetch a repository and normalize it into the internal model.

        Args:
            owner: The repository owner's login.
            name: The repository name.

        Returns:
            The normalized repository metadata.

        Raises:
            GitHubNotFoundError: The repository is not visible.
            GitHubError: The request failed or the payload was unusable.
        """
        payload = await self._client.get_object(f"/repos/{owner}/{name}")
        repository = _to_repository(payload, f"{owner}/{name}")

        logger.info(
            "Fetched GitHub repository",
            extra={
                "repository": repository.full_name,
                "default_branch": repository.default_branch,
                "language": repository.language,
            },
        )
        return repository

    async def get_repository_for_issue(self, ref: GitHubIssueRef) -> GitHubRepository:
        """Fetch the repository an issue belongs to."""
        return await self.get_repository(ref.owner, ref.repository)


class _Owner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str


class _RepositoryPayload(BaseModel):
    """The subset of GitHub's repository payload that ReproGate consumes."""

    model_config = ConfigDict(extra="ignore")

    name: str
    full_name: str
    owner: _Owner
    default_branch: str
    clone_url: str
    html_url: str
    language: str | None = None
    description: str | None = None
    private: bool = False
    archived: bool = False
    fork: bool = False


def _to_repository(payload: JsonObject, slug: str) -> GitHubRepository:
    """Normalize a raw repository payload, rejecting anything unusable."""
    try:
        parsed = _RepositoryPayload.model_validate(payload)
    except PydanticValidationError as exc:
        raise GitHubError(
            f"GitHub returned an unexpected repository payload for {slug}."
        ) from exc

    return GitHubRepository(
        owner=parsed.owner.login,
        name=parsed.name,
        full_name=parsed.full_name,
        default_branch=parsed.default_branch,
        clone_url=parsed.clone_url,
        html_url=parsed.html_url,
        language=parsed.language,
        description=parsed.description,
        is_private=parsed.private,
        is_archived=parsed.archived,
        is_fork=parsed.fork,
    )


class GitRepositoryCloner:
    """Clones repositories into isolated, per-run workspaces.

    Every clone lands in its own directory beneath the sandbox workspace root
    and is removed by :meth:`cleanup` once the run is finished. Git is invoked
    directly via ``execve`` with an argument vector — no shell is involved at
    any point, so repository and branch names can never be interpreted as
    commands.

    This type performs no analysis of what it clones; inspecting the checkout
    belongs to ``app.repository_analysis``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace_root: Path | str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._git_binary = self._settings.git_binary
        self._timeout = float(self._settings.clone_timeout_seconds)
        self._workspace_root = Path(
            workspace_root
            if workspace_root is not None
            else self._settings.sandbox_workspace_root
        ).expanduser()

    @property
    def workspace_root(self) -> Path:
        """The directory every clone workspace is created beneath."""
        return self._workspace_root

    async def clone(
        self,
        repository: GitHubRepository,
        *,
        run_id: UUID | str,
        branch: str | None = None,
        shallow: bool | None = None,
        depth: int | None = None,
        overwrite: bool = False,
    ) -> ClonedRepository:
        """Clone ``repository`` into the workspace for ``run_id``.

        Args:
            repository: Metadata from :class:`GitHubRepositoryService`, which
                supplies the clone URL and the default branch.
            run_id: Identifier of the verification run. Determines the
                workspace directory name and must be unique per run.
            branch: Branch to check out. Defaults to the repository's default
                branch.
            shallow: Whether to truncate history. Defaults to the configured
                ``CLONE_SHALLOW``.
            depth: Commits to retain when shallow. Defaults to ``CLONE_DEPTH``.
            overwrite: Replace a non-empty existing workspace instead of
                failing.

        Returns:
            The local path and clone metadata.

        Raises:
            WorkspaceError: The destination is unusable, unsafe, or occupied.
            RepositoryNotClonableError: The repository or branch is unreachable.
            RepositoryCloneTimeoutError: The clone exceeded its time limit.
            RepositoryCloneError: The clone failed for any other reason.
            ConfigurationError: The git binary is not installed.
        """
        # `None` means "use the default"; anything explicitly passed is
        # validated as given, so an empty string is an error rather than a
        # silent fallback.
        target_branch = repository.default_branch if branch is None else branch
        use_shallow = self._settings.clone_shallow if shallow is None else shallow
        clone_depth = depth if depth is not None else self._settings.clone_depth
        effective_depth = max(1, clone_depth) if use_shallow else None

        clone_url = _validate_clone_url(repository.clone_url)
        _validate_branch(target_branch)

        if repository.is_private:
            # Anonymous clones cannot reach private repositories, and git would
            # otherwise fail with an opaque credential prompt error.
            raise RepositoryNotClonableError(
                f"{repository.full_name} is private; ReproGate clones "
                "anonymously and cannot access it."
            )

        destination = self._prepare_destination(run_id, overwrite=overwrite)

        logger.info(
            "Cloning repository",
            extra={
                "repository": repository.full_name,
                "run_id": str(run_id),
                "branch": target_branch,
                "depth": effective_depth,
                "destination": str(destination),
            },
        )

        started = time.monotonic()
        await self._run_clone(clone_url, destination, target_branch, effective_depth)
        duration_ms = int((time.monotonic() - started) * 1000)

        commit_sha = await self._rev_parse(destination, "HEAD")
        checked_out_branch = await self._rev_parse(destination, "--abbrev-ref", "HEAD")

        cloned = ClonedRepository(
            full_name=repository.full_name,
            path=destination,
            branch=checked_out_branch,
            commit_sha=commit_sha,
            clone_url=clone_url,
            is_shallow=use_shallow,
            depth=effective_depth,
            cloned_at=datetime.now(tz=UTC),
            duration_ms=duration_ms,
        )

        logger.info(
            "Cloned repository",
            extra={
                "repository": cloned.full_name,
                "run_id": str(run_id),
                "branch": cloned.branch,
                "commit_sha": cloned.commit_sha,
                "duration_ms": cloned.duration_ms,
                "destination": str(destination),
            },
        )
        return cloned

    async def cleanup(self, path: Path | ClonedRepository) -> None:
        """Remove a clone workspace. Safe to call more than once.

        Raises:
            WorkspaceError: ``path`` lies outside the workspace root.
        """
        target = path.path if isinstance(path, ClonedRepository) else path
        resolved = self._require_inside_workspace(target)

        if not resolved.exists():
            return

        await asyncio.to_thread(shutil.rmtree, resolved, ignore_errors=True)
        logger.info("Removed clone workspace", extra={"destination": str(resolved)})

    @asynccontextmanager
    async def cloned_workspace(
        self,
        repository: GitHubRepository,
        *,
        run_id: UUID | str,
        **options: object,
    ) -> AsyncIterator[ClonedRepository]:
        """Clone for the duration of the block, then always clean up.

        Accepts the same keyword options as :meth:`clone`.
        """
        cloned = await self.clone(repository, run_id=run_id, **options)  # type: ignore[arg-type]
        try:
            yield cloned
        finally:
            await self.cleanup(cloned.path)

    def _prepare_destination(self, run_id: UUID | str, *, overwrite: bool) -> Path:
        """Resolve, validate, and clear the workspace directory for a run."""
        identifier = str(run_id)
        if not _RUN_ID_RE.match(identifier):
            raise WorkspaceError(
                f"{identifier!r} is not a usable workspace identifier."
            )

        try:
            root = self._workspace_root.resolve()
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"The workspace root {self._workspace_root} could not be created."
            ) from exc

        destination = (root / identifier).resolve()
        if destination == root or not destination.is_relative_to(root):
            raise WorkspaceError(
                f"The workspace for {identifier!r} would fall outside {root}."
            )

        if destination.exists():
            if not destination.is_dir():
                raise WorkspaceError(
                    f"{destination} already exists and is not a directory."
                )
            if any(destination.iterdir()):
                if not overwrite:
                    raise WorkspaceError(
                        f"The workspace {destination} already exists and is not "
                        "empty. Use a fresh run identifier, or pass "
                        "overwrite=True to replace it."
                    )
                logger.warning(
                    "Replacing existing clone workspace",
                    extra={"destination": str(destination)},
                )
                shutil.rmtree(destination, ignore_errors=True)

        return destination

    def _require_inside_workspace(self, path: Path) -> Path:
        """Guard every destructive operation against paths we do not own."""
        root = self._workspace_root.resolve()
        # Non-strict resolution, so an already-deleted path is still checked
        # rather than raising and skipping the guard below.
        resolved = Path(path).resolve()

        if resolved == root or not resolved.is_relative_to(root):
            raise WorkspaceError(
                f"Refusing to operate on {resolved}, which is outside the "
                f"workspace root {root}."
            )
        return resolved

    def _build_clone_args(
        self,
        clone_url: str,
        destination: Path,
        branch: str,
        depth: int | None,
    ) -> list[str]:
        args = [
            self._git_binary,
            # Neutralise any credential helper that could prompt or leak.
            "-c",
            "credential.helper=",
            "clone",
            "--quiet",
            "--single-branch",
            # `--opt=value` form so a value can never be read as a new option.
            f"--branch={branch}",
        ]
        if depth is not None:
            args.append(f"--depth={depth}")
        # `--` terminates options: the URL and path that follow are operands.
        args += ["--", clone_url, str(destination)]
        return args

    async def _run_clone(
        self,
        clone_url: str,
        destination: Path,
        branch: str,
        depth: int | None,
    ) -> None:
        args = self._build_clone_args(clone_url, destination, branch, depth)
        returncode, _, stderr = await self._run_git(args, timeout=self._timeout)

        if returncode == 0:
            return

        # A failed clone can leave a partial checkout behind.
        shutil.rmtree(destination, ignore_errors=True)

        excerpt = _stderr_excerpt(stderr)
        logger.warning(
            "Clone failed",
            extra={
                "clone_url": clone_url,
                "branch": branch,
                "exit_code": returncode,
                "stderr": excerpt,
            },
        )

        lowered = excerpt.lower()
        if any(marker in lowered for marker in _NOT_CLONABLE_MARKERS):
            raise RepositoryNotClonableError(
                f"{clone_url} could not be cloned: {excerpt}"
            )
        raise RepositoryCloneError(
            f"Cloning {clone_url} failed with exit code {returncode}: {excerpt}"
        )

    async def _rev_parse(self, destination: Path, *revision: str) -> str:
        """Read a resolved revision out of a fresh checkout."""
        args = [self._git_binary, "-C", str(destination), "rev-parse", *revision]
        returncode, stdout, stderr = await self._run_git(args, timeout=self._timeout)

        if returncode != 0:
            raise RepositoryCloneError(
                f"Could not inspect the clone at {destination}: "
                f"{_stderr_excerpt(stderr)}"
            )
        return stdout.strip()

    async def _run_git(
        self, args: Sequence[str], *, timeout: float
    ) -> tuple[int, str, str]:
        """Run git with an argument vector, never a shell command string."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_git_env(),
                # Own process group so a timeout can kill git's helper
                # processes (git-remote-https) along with it.
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"The git binary {self._git_binary!r} was not found. "
                "Install git or set GIT_BINARY."
            ) from exc
        except OSError as exc:
            raise RepositoryCloneError(f"git could not be started: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            _kill_process_group(process)
            await process.wait()
            logger.warning(
                "Clone timed out",
                extra={"timeout_seconds": timeout, "command": args[1:2]},
            )
            raise RepositoryCloneTimeoutError(
                f"git did not finish within {timeout:.0f}s."
            ) from None

        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )


def _validate_clone_url(clone_url: str) -> str:
    """Reject clone URLs whose transport we do not trust."""
    parts = urlsplit(clone_url)
    scheme = parts.scheme.lower()

    if scheme not in _ALLOWED_CLONE_SCHEMES:
        raise WorkspaceError(
            f"Refusing to clone over {scheme or 'an unknown'} transport; "
            f"only {', '.join(sorted(_ALLOWED_CLONE_SCHEMES))} is allowed."
        )

    if parts.username or parts.password:
        raise WorkspaceError("The clone URL must not embed credentials.")

    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_CLONE_HOSTS:
        raise WorkspaceError(f"Refusing to clone from host {host!r}.")

    return clone_url


def _validate_branch(branch: str) -> str:
    """Reject branch names that git could read as options or paths."""
    if not _BRANCH_RE.match(branch) or ".." in branch:
        raise WorkspaceError(f"{branch!r} is not a usable branch name.")
    return branch


def _git_env() -> dict[str, str]:
    """A minimal, non-interactive environment for git.

    User and system git configuration is ignored so that settings such as
    ``url.<base>.insteadOf`` cannot silently redirect a clone elsewhere.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill git and everything it spawned; ignore an already-dead process."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with suppress(ProcessLookupError):
            process.kill()


def _stderr_excerpt(stderr: str) -> str:
    collapsed = " ".join(stderr.split())
    if len(collapsed) <= _STDERR_EXCERPT_LIMIT:
        return collapsed
    return f"{collapsed[:_STDERR_EXCERPT_LIMIT]}…"
