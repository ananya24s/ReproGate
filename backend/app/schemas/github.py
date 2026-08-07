"""Normalized GitHub domain models.

These are ReproGate's internal representation of GitHub data. The shape of the
GitHub REST payloads stays inside ``app.github``; nothing outside that module
should depend on GitHub's field names.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class GitHubIssueState(str, Enum):
    """Whether an issue is still open."""

    OPEN = "open"
    CLOSED = "closed"


class GitHubIssueRef(BaseModel):
    """The coordinates of a single issue, parsed from its URL."""

    model_config = ConfigDict(frozen=True)

    owner: str
    repository: str
    issue_number: int = Field(gt=0)

    @property
    def full_name(self) -> str:
        """The ``owner/repository`` slug used throughout the GitHub API."""
        return f"{self.owner}/{self.repository}"

    def __str__(self) -> str:
        return f"{self.full_name}#{self.issue_number}"


class GitHubRepository(BaseModel):
    """Repository metadata needed to plan a verification run."""

    model_config = ConfigDict(frozen=True)

    owner: str
    name: str
    full_name: str
    default_branch: str
    clone_url: str
    html_url: str
    language: str | None = None
    description: str | None = None
    is_private: bool = False
    is_archived: bool = False
    is_fork: bool = False


class GitHubIssue(BaseModel):
    """Issue metadata describing the behavior to reproduce."""

    model_config = ConfigDict(frozen=True)

    number: int = Field(gt=0)
    title: str
    body: str | None
    state: GitHubIssueState
    author: str | None
    labels: tuple[str, ...] = ()
    html_url: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


class GitHubIssueLookupRequest(BaseModel):
    """A request to resolve a GitHub issue URL into its metadata."""

    model_config = ConfigDict(extra="forbid")

    # Deliberately a plain string rather than ``HttpUrl``: ``parse_issue_url``
    # is the single authority on what a usable issue URL is, and its messages
    # are far more actionable than a generic URL validation failure.
    issue_url: str = Field(
        min_length=1,
        max_length=2048,
        description="URL of the GitHub issue to resolve.",
        examples=["https://github.com/owner/repo/issues/42"],
    )


class GitHubIssueLookup(BaseModel):
    """Repository and issue metadata resolved from an issue URL."""

    repository: GitHubRepository
    issue: GitHubIssue
