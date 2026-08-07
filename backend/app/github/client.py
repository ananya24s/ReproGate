"""Low-level authenticated GitHub HTTP client.

Owns transport concerns only: base URL, authentication, retries, rate-limit
handling, and error translation. It exposes no domain vocabulary.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Final

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    GitHubAuthenticationError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

JsonObject = dict[str, Any]

_GITHUB_ACCEPT: Final = "application/vnd.github+json"
_GITHUB_API_VERSION: Final = "2022-11-28"

#: Server-side faults that are worth repeating verbatim.
_RETRYABLE_STATUS_CODES: Final = frozenset({500, 502, 503, 504})

#: Statuses GitHub uses for both permission denials and rate limiting.
_THROTTLED_STATUS_CODES: Final = frozenset({403, 429})


class GitHubClient:
    """An async HTTP client for the GitHub REST API.

    The client retries transient failures — connection errors, timeouts, 5xx
    responses, and rate limits whose reset is near — and converts every
    terminal failure into a :class:`~app.core.exceptions.GitHubError`.

    Instances hold a connection pool and must be closed. Use it as an async
    context manager, or call :meth:`aclose`.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._max_retries = max(0, self._settings.github_max_retries)
        self._backoff = max(0.0, self._settings.github_retry_backoff_seconds)
        self._max_retry_wait = max(0.0, self._settings.github_max_retry_wait_seconds)

        headers = {
            "Accept": _GITHUB_ACCEPT,
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            "User-Agent": self._settings.app_name,
        }
        if self._settings.github_token:
            headers["Authorization"] = f"Bearer {self._settings.github_token}"
        else:
            logger.warning(
                "GITHUB_TOKEN is not set; requests are subject to GitHub's "
                "unauthenticated rate limit and cannot reach private repositories."
            )

        self._client = httpx.AsyncClient(
            base_url=self._settings.github_api_url,
            headers=headers,
            timeout=httpx.Timeout(self._settings.github_request_timeout_seconds),
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def get_object(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        """GET ``path`` and return the decoded JSON object.

        Args:
            path: Path relative to the configured API base URL.
            params: Optional query parameters.

        Raises:
            GitHubNotFoundError: The resource does not exist or is not visible.
            GitHubAuthenticationError: GitHub rejected the credentials.
            GitHubRateLimitError: The rate limit is exhausted.
            GitHubError: Any other transport or API failure.
        """
        response = await self._request("GET", path, params=params)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(
                f"GitHub returned a non-JSON response for {path}."
            ) from exc

        if not isinstance(payload, dict):
            raise GitHubError(
                f"GitHub returned {type(payload).__name__} for {path}, "
                "but an object was expected."
            )
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform a request, retrying transient failures."""
        attempt = 0

        while True:
            try:
                response = await self._client.request(method, path, params=params)
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    await self._sleep_before_retry(
                        self._backoff_delay(attempt), attempt, path, reason="timeout"
                    )
                    attempt += 1
                    continue
                raise GitHubError(f"The GitHub request to {path} timed out.") from exc
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    await self._sleep_before_retry(
                        self._backoff_delay(attempt),
                        attempt,
                        path,
                        reason="transport_error",
                    )
                    attempt += 1
                    continue
                raise GitHubError(
                    f"The GitHub request to {path} could not be completed."
                ) from exc

            if response.is_success:
                return response

            delay = self._retry_delay(response, attempt)
            if delay is not None and attempt < self._max_retries:
                await self._sleep_before_retry(
                    delay, attempt, path, reason=f"status_{response.status_code}"
                )
                attempt += 1
                continue

            raise self._translate_error(response, path)

    async def _sleep_before_retry(
        self, delay: float, attempt: int, path: str, *, reason: str
    ) -> None:
        logger.warning(
            "Retrying GitHub request",
            extra={
                "path": path,
                "attempt": attempt + 1,
                "max_retries": self._max_retries,
                "delay_seconds": round(delay, 3),
                "reason": reason,
            },
        )
        await asyncio.sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped at the maximum wait."""
        base = min(self._backoff * (2.0**attempt), self._max_retry_wait)
        return base + random.uniform(0, self._backoff)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        """Return how long to wait before retrying, or ``None`` if terminal."""
        if response.status_code in _RETRYABLE_STATUS_CODES:
            return self._backoff_delay(attempt)

        if response.status_code in _THROTTLED_STATUS_CODES and _is_rate_limited(
            response
        ):
            wait = _throttle_wait_seconds(response)
            # Waiting out a limit that resets an hour from now is worse than
            # failing fast and letting the caller decide.
            if wait is not None and wait <= self._max_retry_wait:
                return max(wait, 0.0)

        return None

    def _translate_error(self, response: httpx.Response, path: str) -> GitHubError:
        """Map an error response onto a domain exception."""
        message = _error_message(response)
        status_code = response.status_code

        if status_code in _THROTTLED_STATUS_CODES and _is_rate_limited(response):
            logger.warning(
                "GitHub rate limit exhausted",
                extra={"path": path, "status_code": status_code},
            )
            return GitHubRateLimitError(
                f"The GitHub API rate limit is exhausted: {message}"
            )

        if status_code == 401:
            return GitHubAuthenticationError(
                f"GitHub rejected the configured credentials: {message}"
            )

        if status_code == 404:
            return GitHubNotFoundError(f"GitHub resource {path} was not found.")

        if status_code == 403:
            # Without a rate-limit signal, 403 means the token lacks access.
            return GitHubAuthenticationError(
                f"GitHub denied access to {path}: {message}"
            )

        logger.warning(
            "GitHub request failed",
            extra={"path": path, "status_code": status_code},
        )
        return GitHubError(
            f"The GitHub request to {path} failed with status {status_code}: {message}"
        )


def _error_message(response: httpx.Response) -> str:
    """Extract GitHub's human-readable error message, if it sent one."""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message

    return response.reason_phrase or f"HTTP {response.status_code}"


def _is_rate_limited(response: httpx.Response) -> bool:
    """Distinguish rate limiting from an ordinary permission denial."""
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if "retry-after" in response.headers:
        return True
    # Secondary rate limits are reported only in the message body.
    return "rate limit" in _error_message(response).lower()


def _throttle_wait_seconds(response: httpx.Response) -> float | None:
    """Seconds until the limit resets, from ``Retry-After`` or the reset epoch."""
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass

    reset_at = response.headers.get("x-ratelimit-reset")
    if reset_at:
        try:
            reset_epoch = float(reset_at)
        except ValueError:
            return None
        now = datetime.now(tz=UTC).timestamp()
        return reset_epoch - now

    return None
