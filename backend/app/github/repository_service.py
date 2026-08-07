"""Reads GitHub repository data: metadata, default branch, commits, and source
code retrieval for a given revision.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import GitHubError
from app.core.logging import get_logger
from app.github.client import GitHubClient, JsonObject
from app.schemas.github import GitHubIssueRef, GitHubRepository

logger = get_logger(__name__)


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
