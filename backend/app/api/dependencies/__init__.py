"""Reusable FastAPI dependencies for the API layer."""

from app.api.dependencies.database import SessionDep
from app.api.dependencies.github import (
    GitHubClientDep,
    IssueServiceDep,
    RepositoryServiceDep,
)

__all__ = [
    "GitHubClientDep",
    "IssueServiceDep",
    "RepositoryServiceDep",
    "SessionDep",
]
