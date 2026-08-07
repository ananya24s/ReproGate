"""Unit tests for repository cloning.

These drive the real ``git`` binary against a repository created on disk, so
the argument vector, exit-code handling, and workspace lifecycle are all
exercised for real. Only the transport allow-list is relaxed, so that
``file://`` URLs can stand in for github.com.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    RepositoryCloneError,
    RepositoryCloneTimeoutError,
    RepositoryNotClonableError,
    WorkspaceError,
)
from app.github import repository_service
from app.github.repository_service import GitRepositoryCloner
from app.schemas.github import ClonedRepository, GitHubRepository

RUN_ID = "3f1c2d34-5b6a-47e8-9c10-2f3a4b5c6d7e"
DEFAULT_BRANCH = "trunk"


def _git(*args: str, cwd: Path) -> str:
    """Run git for test setup and assertions."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    return result.stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A local git repository with two commits on a non-default branch name."""
    origin = tmp_path / "origin"
    origin.mkdir()

    _git("init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}", cwd=origin)
    (origin / "README.md").write_text("first\n")
    _git("add", "README.md", cwd=origin)
    _git("commit", "--quiet", "-m", "first", cwd=origin)

    (origin / "README.md").write_text("second\n")
    _git("commit", "--quiet", "-am", "second", cwd=origin)

    # A second branch that must NOT be the one checked out.
    _git("branch", "side-branch", cwd=origin)
    return origin


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        sandbox_workspace_root=str(tmp_path / "workspaces"),
        clone_timeout_seconds=60,
        clone_shallow=True,
        clone_depth=1,
    )


@pytest.fixture
def allow_local_clones(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permit ``file://`` URLs, which have no host, for the duration of a test."""
    monkeypatch.setattr(
        repository_service, "_ALLOWED_CLONE_SCHEMES", frozenset({"file"})
    )
    monkeypatch.setattr(repository_service, "_ALLOWED_CLONE_HOSTS", frozenset({""}))


@pytest.fixture
def cloner(settings: Settings) -> GitRepositoryCloner:
    return GitRepositoryCloner(settings)


def make_repository(source: Path, **overrides: object) -> GitHubRepository:
    defaults: dict[str, object] = {
        "owner": "octocat",
        "name": "hello-world",
        "full_name": "octocat/hello-world",
        "default_branch": DEFAULT_BRANCH,
        "clone_url": source.as_uri(),
        "html_url": "https://github.com/octocat/hello-world",
    }
    defaults.update(overrides)
    return GitHubRepository.model_validate(defaults)


# --------------------------------------------------------------------------
# Successful clone
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_local_clones")
async def test_clone_succeeds_and_returns_metadata(
    cloner: GitRepositoryCloner, source_repo: Path, workspace_root: Path
) -> None:
    expected_sha = _git("rev-parse", "HEAD", cwd=source_repo)

    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    assert isinstance(cloned, ClonedRepository)
    assert cloned.path == workspace_root.resolve() / RUN_ID
    assert cloned.path.is_dir()
    assert (cloned.path / ".git").is_dir()
    assert (cloned.path / "README.md").read_text() == "second\n"

    assert cloned.full_name == "octocat/hello-world"
    assert cloned.commit_sha == expected_sha
    assert cloned.is_shallow is True
    assert cloned.depth == 1
    assert cloned.duration_ms >= 0
    assert isinstance(cloned.cloned_at, datetime)
    assert cloned.cloned_at.tzinfo is not None


@pytest.mark.usefixtures("allow_local_clones")
async def test_clone_checks_out_the_default_branch(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    assert cloned.branch == DEFAULT_BRANCH
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cloned.path) == DEFAULT_BRANCH


@pytest.mark.usefixtures("allow_local_clones")
async def test_clone_is_shallow_by_default(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    assert _git("rev-list", "--count", "HEAD", cwd=cloned.path) == "1"


@pytest.mark.usefixtures("allow_local_clones")
async def test_full_clone_retains_history(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    cloned = await cloner.clone(
        make_repository(source_repo), run_id=RUN_ID, shallow=False
    )

    assert cloned.is_shallow is False
    assert cloned.depth is None
    assert _git("rev-list", "--count", "HEAD", cwd=cloned.path) == "2"


@pytest.mark.usefixtures("allow_local_clones")
async def test_each_run_gets_its_own_workspace(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    repository = make_repository(source_repo)

    first = await cloner.clone(repository, run_id="run-one")
    second = await cloner.clone(repository, run_id="run-two")

    assert first.path != second.path
    assert first.path.is_dir()
    assert second.path.is_dir()


# --------------------------------------------------------------------------
# Invalid repository
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_local_clones")
async def test_missing_repository_raises_not_clonable(
    cloner: GitRepositoryCloner, tmp_path: Path
) -> None:
    missing = make_repository(tmp_path / "does-not-exist")

    with pytest.raises(RepositoryNotClonableError):
        await cloner.clone(missing, run_id=RUN_ID)


@pytest.mark.usefixtures("allow_local_clones")
async def test_missing_branch_raises_not_clonable(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    repository = make_repository(source_repo, default_branch="no-such-branch")

    with pytest.raises(RepositoryNotClonableError):
        await cloner.clone(repository, run_id=RUN_ID)


@pytest.mark.usefixtures("allow_local_clones")
async def test_failed_clone_leaves_no_partial_workspace(
    cloner: GitRepositoryCloner, tmp_path: Path, workspace_root: Path
) -> None:
    with pytest.raises(RepositoryNotClonableError):
        await cloner.clone(make_repository(tmp_path / "nope"), run_id=RUN_ID)

    assert not (workspace_root / RUN_ID).exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_private_repository_is_rejected(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    repository = make_repository(source_repo, is_private=True)

    with pytest.raises(RepositoryNotClonableError, match="private"):
        await cloner.clone(repository, run_id=RUN_ID)


@pytest.mark.parametrize(
    "clone_url",
    [
        pytest.param("ext::sh -c 'touch /tmp/pwned'", id="ext-command-helper"),
        pytest.param("file:///etc", id="local-filesystem"),
        pytest.param("git://github.com/owner/repo.git", id="unauthenticated-git"),
        pytest.param("ssh://git@github.com/owner/repo.git", id="ssh"),
        pytest.param("https://evil.example/owner/repo.git", id="untrusted-host"),
        pytest.param("https://user:pw@github.com/o/r.git", id="embedded-credentials"),
    ],
)
async def test_untrusted_clone_urls_are_refused(
    cloner: GitRepositoryCloner, source_repo: Path, clone_url: str
) -> None:
    repository = make_repository(source_repo, clone_url=clone_url)

    with pytest.raises(WorkspaceError):
        await cloner.clone(repository, run_id=RUN_ID)


@pytest.mark.usefixtures("allow_local_clones")
@pytest.mark.parametrize(
    "branch",
    [
        pytest.param("--upload-pack=touch /tmp/pwned", id="option-injection"),
        pytest.param("../escape", id="path-traversal"),
        pytest.param("has space", id="whitespace"),
        pytest.param("", id="empty"),
    ],
)
async def test_unsafe_branch_names_are_refused(
    cloner: GitRepositoryCloner, source_repo: Path, branch: str
) -> None:
    with pytest.raises(WorkspaceError):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID, branch=branch)


@pytest.mark.usefixtures("allow_local_clones")
@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("../outside", id="parent-traversal"),
        pytest.param("nested/path", id="separator"),
        pytest.param("/absolute", id="absolute"),
        pytest.param("", id="empty"),
        pytest.param(".hidden", id="leading-dot"),
    ],
)
async def test_unsafe_run_identifiers_are_refused(
    cloner: GitRepositoryCloner, source_repo: Path, run_id: str
) -> None:
    with pytest.raises(WorkspaceError):
        await cloner.clone(make_repository(source_repo), run_id=run_id)


async def test_missing_git_binary_is_a_configuration_error(
    settings: Settings, source_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        repository_service, "_ALLOWED_CLONE_SCHEMES", frozenset({"file"})
    )
    monkeypatch.setattr(repository_service, "_ALLOWED_CLONE_HOSTS", frozenset({""}))
    cloner = GitRepositoryCloner(
        settings.model_copy(update={"git_binary": "/nonexistent/git"})
    )

    with pytest.raises(ConfigurationError, match="git"):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID)


# --------------------------------------------------------------------------
# Timeout
# --------------------------------------------------------------------------


@pytest.fixture
def slow_git(tmp_path: Path) -> str:
    """A stand-in git that never finishes, to force the timeout path."""
    script = tmp_path / "slow-git"
    script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(120)\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.usefixtures("allow_local_clones")
async def test_clone_times_out(
    settings: Settings, source_repo: Path, slow_git: str
) -> None:
    cloner = GitRepositoryCloner(
        settings.model_copy(update={"git_binary": slow_git, "clone_timeout_seconds": 1})
    )

    with pytest.raises(RepositoryCloneTimeoutError):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID)


@pytest.mark.usefixtures("allow_local_clones")
async def test_timeout_does_not_leave_the_workspace_behind(
    settings: Settings, source_repo: Path, slow_git: str, workspace_root: Path
) -> None:
    cloner = GitRepositoryCloner(
        settings.model_copy(update={"git_binary": slow_git, "clone_timeout_seconds": 1})
    )

    with pytest.raises(RepositoryCloneTimeoutError):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    await cloner.cleanup(workspace_root / RUN_ID)
    assert not (workspace_root / RUN_ID).exists()


# --------------------------------------------------------------------------
# Existing destination
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_local_clones")
async def test_non_empty_destination_is_refused(
    cloner: GitRepositoryCloner, source_repo: Path, workspace_root: Path
) -> None:
    destination = workspace_root / RUN_ID
    destination.mkdir(parents=True)
    (destination / "keep.txt").write_text("do not delete me")

    with pytest.raises(WorkspaceError, match="already exists"):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    # The refusal must not have destroyed what was already there.
    assert (destination / "keep.txt").read_text() == "do not delete me"


@pytest.mark.usefixtures("allow_local_clones")
async def test_empty_destination_is_reused(
    cloner: GitRepositoryCloner, source_repo: Path, workspace_root: Path
) -> None:
    (workspace_root / RUN_ID).mkdir(parents=True)

    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    assert (cloned.path / "README.md").exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_overwrite_replaces_an_existing_destination(
    cloner: GitRepositoryCloner, source_repo: Path, workspace_root: Path
) -> None:
    destination = workspace_root / RUN_ID
    destination.mkdir(parents=True)
    (destination / "stale.txt").write_text("from a previous run")

    cloned = await cloner.clone(
        make_repository(source_repo), run_id=RUN_ID, overwrite=True
    )

    assert not (cloned.path / "stale.txt").exists()
    assert (cloned.path / "README.md").exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_destination_occupied_by_a_file_is_refused(
    cloner: GitRepositoryCloner, source_repo: Path, workspace_root: Path
) -> None:
    workspace_root.mkdir(parents=True)
    (workspace_root / RUN_ID).write_text("not a directory")

    with pytest.raises(WorkspaceError, match="not a directory"):
        await cloner.clone(make_repository(source_repo), run_id=RUN_ID)


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("allow_local_clones")
async def test_cleanup_removes_the_workspace(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)
    assert cloned.path.exists()

    await cloner.cleanup(cloned)

    assert not cloned.path.exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_cleanup_is_idempotent(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    cloned = await cloner.clone(make_repository(source_repo), run_id=RUN_ID)

    await cloner.cleanup(cloned.path)
    await cloner.cleanup(cloned.path)

    assert not cloned.path.exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_cleanup_leaves_sibling_workspaces_alone(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    repository = make_repository(source_repo)
    first = await cloner.clone(repository, run_id="run-one")
    second = await cloner.clone(repository, run_id="run-two")

    await cloner.cleanup(first)

    assert not first.path.exists()
    assert second.path.is_dir()


@pytest.mark.parametrize(
    "outside",
    [
        pytest.param("/", id="filesystem-root"),
        pytest.param("/etc", id="system-directory"),
        pytest.param("..", id="workspace-parent"),
        pytest.param(".", id="workspace-root-itself"),
    ],
)
async def test_cleanup_refuses_paths_outside_the_workspace(
    cloner: GitRepositoryCloner, workspace_root: Path, outside: str
) -> None:
    workspace_root.mkdir(parents=True)
    target = workspace_root / outside if outside in {"..", "."} else Path(outside)

    with pytest.raises(WorkspaceError, match="outside"):
        await cloner.cleanup(target)


@pytest.mark.usefixtures("allow_local_clones")
async def test_cloned_workspace_cleans_up_on_success(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    async with cloner.cloned_workspace(
        make_repository(source_repo), run_id=RUN_ID
    ) as cloned:
        assert cloned.path.is_dir()
        inside = cloned.path

    assert not inside.exists()


@pytest.mark.usefixtures("allow_local_clones")
async def test_cloned_workspace_cleans_up_after_a_failure(
    cloner: GitRepositoryCloner, source_repo: Path
) -> None:
    inside: Path | None = None

    with pytest.raises(RepositoryCloneError):
        async with cloner.cloned_workspace(
            make_repository(source_repo), run_id=RUN_ID
        ) as cloned:
            inside = cloned.path
            raise RepositoryCloneError("something downstream went wrong")

    assert inside is not None
    assert not inside.exists()
