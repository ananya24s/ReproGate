"""GitHub service dependencies.

The :class:`~app.github.client.GitHubClient` owns a connection pool, so a
single instance is created during application startup and shared by every
request. The thin services around it are cheap and are built per request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.exceptions import ConfigurationError
from app.github.client import GitHubClient
from app.github.issue_service import GitHubIssueService
from app.github.repository_service import GitHubRepositoryService


def get_github_client(request: Request) -> GitHubClient:
    """Return the application-lifetime GitHub client."""
    client = getattr(request.app.state, "github_client", None)
    if not isinstance(client, GitHubClient):
        raise ConfigurationError(
            "The GitHub client was not initialized during application startup."
        )
    return client


GitHubClientDep = Annotated[GitHubClient, Depends(get_github_client)]


def get_issue_service(client: GitHubClientDep) -> GitHubIssueService:
    return GitHubIssueService(client)


def get_repository_service(client: GitHubClientDep) -> GitHubRepositoryService:
    return GitHubRepositoryService(client)


IssueServiceDep = Annotated[GitHubIssueService, Depends(get_issue_service)]
RepositoryServiceDep = Annotated[
    GitHubRepositoryService, Depends(get_repository_service)
]
