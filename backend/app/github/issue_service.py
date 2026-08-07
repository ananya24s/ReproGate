"""Reads GitHub issue data: title, description, state, author, and labels.

Also owns parsing and validation of the GitHub issue URLs submitted by users,
since the URL is the entry point of the verification workflow.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import GitHubError, InvalidIssueURLError
from app.core.logging import get_logger
from app.github.client import GitHubClient, JsonObject
from app.schemas.github import GitHubIssue, GitHubIssueRef, GitHubIssueState

logger = get_logger(__name__)

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_ALLOWED_HOSTS: Final = frozenset({"github.com", "www.github.com"})

# GitHub logins are 1-39 characters and may not begin or end with a hyphen.
_OWNER_PATTERN: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
# Repository names allow letters, digits, hyphens, underscores, and dots.
_REPOSITORY_PATTERN: Final = r"[A-Za-z0-9._-]{1,100}"
# Bounded to ten digits; anything longer is not a real issue number.
_ISSUE_NUMBER_PATTERN: Final = r"[1-9][0-9]{0,9}"

_ISSUE_PATH_RE: Final = re.compile(
    rf"^/(?P<owner>{_OWNER_PATTERN})"
    rf"/(?P<repository>{_REPOSITORY_PATTERN})"
    rf"/issues/(?P<issue_number>{_ISSUE_NUMBER_PATTERN})$"
)

_PULL_REQUEST_PATH_RE: Final = re.compile(r"^/[^/]+/[^/]+/pulls?/[0-9]+$")

#: Repository names that are path segments rather than real names.
_RESERVED_REPOSITORY_NAMES: Final = frozenset({".", ".."})


def parse_issue_url(url: str) -> GitHubIssueRef:
    """Parse and validate a GitHub issue URL.

    Accepts the canonical issue URL form, with or without a trailing slash,
    and ignores any query string or fragment — GitHub appends
    ``#issuecomment-…`` when linking to a specific comment.

    Args:
        url: A GitHub issue URL, for example
            ``https://github.com/owner/repo/issues/42``.

    Returns:
        The parsed owner, repository, and issue number.

    Raises:
        InvalidIssueURLError: The value is not a well-formed GitHub issue URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidIssueURLError("A GitHub issue URL is required.")

    candidate = url.strip()

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidIssueURLError(f"{candidate!r} is not a valid URL.") from exc

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidIssueURLError("The issue URL must start with http:// or https://.")

    # Reject credentials outright: `https://github.com@evil.example` parses to
    # a host of evil.example and would otherwise read as a GitHub URL.
    if parts.username or parts.password:
        raise InvalidIssueURLError("The issue URL must not contain credentials.")

    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidIssueURLError(
            f"Only github.com issue URLs are supported, but the host was {host!r}."
        )

    path = parts.path.rstrip("/") or "/"

    if _PULL_REQUEST_PATH_RE.match(path):
        raise InvalidIssueURLError(
            "Pull request URLs are not supported; provide an issue URL."
        )

    match = _ISSUE_PATH_RE.match(path)
    if match is None:
        raise InvalidIssueURLError(
            "The issue URL must look like "
            "https://github.com/<owner>/<repository>/issues/<number>."
        )

    repository = match.group("repository")
    if repository in _RESERVED_REPOSITORY_NAMES:
        raise InvalidIssueURLError(f"{repository!r} is not a valid repository name.")

    return GitHubIssueRef(
        owner=match.group("owner"),
        repository=repository,
        issue_number=int(match.group("issue_number")),
    )


class GitHubIssueService:
    """Fetches issue metadata from the GitHub REST API."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def get_issue(self, ref: GitHubIssueRef) -> GitHubIssue:
        """Fetch a single issue and normalize it into the internal model.

        Args:
            ref: The issue coordinates, typically from :func:`parse_issue_url`.

        Returns:
            The normalized issue.

        Raises:
            InvalidIssueURLError: The reference points at a pull request.
            GitHubNotFoundError: The issue or repository is not visible.
            GitHubError: The request failed or the payload was unusable.
        """
        payload = await self._client.get_object(
            f"/repos/{ref.owner}/{ref.repository}/issues/{ref.issue_number}"
        )
        issue = _to_issue(payload, ref)

        logger.info(
            "Fetched GitHub issue",
            extra={
                "repository": ref.full_name,
                "issue_number": issue.number,
                "issue_state": issue.state.value,
            },
        )
        return issue


class _Label(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class _User(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str


class _IssuePayload(BaseModel):
    """The subset of GitHub's issue payload that ReproGate consumes."""

    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    body: str | None = None
    state: GitHubIssueState
    html_url: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    user: _User | None = None
    labels: list[_Label | str] = []
    # Present only when the "issue" is really a pull request.
    pull_request: dict[str, Any] | None = None


def _to_issue(payload: JsonObject, ref: GitHubIssueRef) -> GitHubIssue:
    """Normalize a raw issue payload, rejecting anything unusable."""
    try:
        parsed = _IssuePayload.model_validate(payload)
    except PydanticValidationError as exc:
        raise GitHubError(
            f"GitHub returned an unexpected issue payload for {ref}."
        ) from exc

    if parsed.pull_request is not None:
        raise InvalidIssueURLError(
            f"{ref} is a pull request; ReproGate verifies issues."
        )

    return GitHubIssue(
        number=parsed.number,
        title=parsed.title,
        body=parsed.body,
        state=parsed.state,
        author=parsed.user.login if parsed.user else None,
        labels=tuple(
            label if isinstance(label, str) else label.name for label in parsed.labels
        ),
        html_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        closed_at=parsed.closed_at,
    )
