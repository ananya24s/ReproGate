"""Unit tests for GitHub issue URL parsing and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import InvalidIssueURLError, ValidationError
from app.github.issue_service import parse_issue_url
from app.schemas.github import GitHubIssueRef


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/owner/repo/issues/42",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=42),
        ),
        (
            "http://github.com/owner/repo/issues/1",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=1),
        ),
        (
            "https://www.github.com/owner/repo/issues/7",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=7),
        ),
        pytest.param(
            "https://GitHub.COM/owner/repo/issues/9",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=9),
            id="host-is-case-insensitive",
        ),
        pytest.param(
            "https://github.com/owner/repo/issues/42/",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=42),
            id="trailing-slash",
        ),
        pytest.param(
            "  https://github.com/owner/repo/issues/42  ",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=42),
            id="surrounding-whitespace",
        ),
        pytest.param(
            "https://github.com/owner/repo/issues/42#issuecomment-1234567",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=42),
            id="comment-fragment-ignored",
        ),
        pytest.param(
            "https://github.com/owner/repo/issues/42?utm_source=slack",
            GitHubIssueRef(owner="owner", repository="repo", issue_number=42),
            id="query-string-ignored",
        ),
        pytest.param(
            "https://github.com/my-org/my.repo_name-2/issues/305",
            GitHubIssueRef(
                owner="my-org", repository="my.repo_name-2", issue_number=305
            ),
            id="punctuation-in-names",
        ),
        pytest.param(
            "https://github.com/a/b/issues/9999999999",
            GitHubIssueRef(owner="a", repository="b", issue_number=9999999999),
            id="single-character-names-and-large-number",
        ),
    ],
)
def test_parses_valid_issue_urls(url: str, expected: GitHubIssueRef) -> None:
    assert parse_issue_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("not a url", id="not-a-url"),
        pytest.param("github.com/owner/repo/issues/1", id="missing-scheme"),
        pytest.param("ftp://github.com/owner/repo/issues/1", id="wrong-scheme"),
        pytest.param(
            "javascript:alert(1)//github.com/o/r/issues/1", id="script-scheme"
        ),
        pytest.param("https://gitlab.com/owner/repo/issues/1", id="wrong-host"),
        pytest.param(
            "https://github.com.evil.example/owner/repo/issues/1",
            id="lookalike-host-suffix",
        ),
        pytest.param(
            "https://notgithub.com/owner/repo/issues/1", id="lookalike-host-prefix"
        ),
        pytest.param(
            "https://github.com@evil.example/owner/repo/issues/1",
            id="credentials-spoof-host",
        ),
        pytest.param("https://github.com/owner/repo", id="repository-url"),
        pytest.param("https://github.com/owner/repo/issues", id="issue-list-url"),
        pytest.param("https://github.com/owner/repo/issues/", id="no-issue-number"),
        pytest.param("https://github.com/owner/repo/issues/abc", id="non-numeric"),
        pytest.param("https://github.com/owner/repo/issues/0", id="zero"),
        pytest.param("https://github.com/owner/repo/issues/-1", id="negative"),
        pytest.param("https://github.com/owner/repo/issues/007", id="leading-zeros"),
        pytest.param(
            "https://github.com/owner/repo/issues/12345678901", id="number-too-long"
        ),
        pytest.param(
            "https://github.com/owner/repo/issues/1/comments", id="extra-path-segment"
        ),
        pytest.param("https://github.com/repo/issues/1", id="missing-owner"),
        pytest.param(
            "https://github.com/-owner/repo/issues/1", id="owner-leading-dash"
        ),
        pytest.param(
            "https://github.com/owner-/repo/issues/1", id="owner-trailing-dash"
        ),
        pytest.param(
            f"https://github.com/{'o' * 40}/repo/issues/1", id="owner-too-long"
        ),
        pytest.param("https://github.com/owner/./issues/1", id="dot-repository"),
        pytest.param("https://github.com/owner/../issues/1", id="dotdot-repository"),
        pytest.param(
            "https://github.com/owner/re po/issues/1", id="space-in-repository"
        ),
    ],
)
def test_rejects_invalid_issue_urls(url: str) -> None:
    with pytest.raises(InvalidIssueURLError):
        parse_issue_url(url)


def test_rejects_pull_request_urls_with_a_specific_message() -> None:
    with pytest.raises(InvalidIssueURLError, match=r"[Pp]ull request"):
        parse_issue_url("https://github.com/owner/repo/pull/42")


def test_invalid_url_error_is_a_domain_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        parse_issue_url("https://gitlab.com/owner/repo/issues/1")

    error = exc_info.value
    assert error.status_code == 422
    assert error.error_code == "invalid_issue_url"
    assert error.message


def test_parsed_reference_exposes_the_repository_slug() -> None:
    ref = parse_issue_url("https://github.com/octocat/hello-world/issues/349")

    assert ref.full_name == "octocat/hello-world"
    assert str(ref) == "octocat/hello-world#349"


def test_parsed_reference_is_immutable() -> None:
    ref = parse_issue_url("https://github.com/owner/repo/issues/1")

    with pytest.raises(PydanticValidationError):
        ref.owner = "someone-else"  # type: ignore[misc]
