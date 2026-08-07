"""HTTP endpoints for resolving GitHub issues.

Resolution is a read-only lookup: it creates no verification run, persists
nothing, and clones nothing. It exists so the user can confirm they submitted
the issue they meant before a run is started.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from app.api.dependencies.github import IssueServiceDep, RepositoryServiceDep
from app.github.issue_service import parse_issue_url
from app.schemas.github import GitHubIssueLookup, GitHubIssueLookupRequest

router = APIRouter(prefix="/issues", tags=["issues"])


@router.post(
    "/resolve",
    response_model=GitHubIssueLookup,
    summary="Resolve a GitHub issue URL",
)
async def resolve_issue(
    payload: GitHubIssueLookupRequest,
    issue_service: IssueServiceDep,
    repository_service: RepositoryServiceDep,
) -> GitHubIssueLookup:
    """Resolve an issue URL into repository and issue metadata.

    Raises:
        InvalidIssueURLError: The URL is malformed or points at a pull request.
        GitHubNotFoundError: The repository or issue is not visible.
        GitHubError: GitHub could not be reached or returned an unusable reply.
    """
    ref = parse_issue_url(payload.issue_url)

    # Independent reads; gather propagates the first failure unwrapped, which
    # keeps the domain exception intact for the application error handler.
    repository, issue = await asyncio.gather(
        repository_service.get_repository_for_issue(ref),
        issue_service.get_issue(ref),
    )

    return GitHubIssueLookup(repository=repository, issue=issue)
