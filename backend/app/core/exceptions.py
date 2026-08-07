"""Application exception types and their HTTP representation.

Domain modules raise :class:`ReproGateError` subclasses. The API layer never
translates exceptions by hand; :func:`register_exception_handlers` maps them to
responses once, for the whole application.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class ReproGateError(Exception):
    """Base class for every error raised by ReproGate."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or type(self).message
        super().__init__(self.message)


class ConfigurationError(ReproGateError):
    """Raised when the application is misconfigured."""

    error_code = "configuration_error"
    message = "The application is misconfigured."


class NotFoundError(ReproGateError):
    """Raised when a requested resource does not exist."""

    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"
    message = "The requested resource was not found."


class ValidationError(ReproGateError):
    """Raised when input is syntactically valid but semantically rejected."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "validation_error"
    message = "The request could not be processed."


class ConflictError(ReproGateError):
    """Raised when an operation conflicts with the current resource state."""

    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ExternalServiceError(ReproGateError):
    """Raised when an upstream dependency fails."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_code = "external_service_error"
    message = "An upstream service failed."


class GitHubError(ExternalServiceError):
    """Raised when the GitHub API cannot satisfy a request."""

    error_code = "github_error"
    message = "The GitHub API request failed."


class LLMError(ExternalServiceError):
    """Raised when the configured LLM provider cannot satisfy a request."""

    error_code = "llm_error"
    message = "The LLM request failed."


class SandboxError(ReproGateError):
    """Raised when sandboxed execution cannot be completed."""

    error_code = "sandbox_error"
    message = "Sandboxed execution failed."


class SandboxTimeoutError(SandboxError):
    """Raised when sandboxed execution exceeds its time limit."""

    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "sandbox_timeout"
    message = "Sandboxed execution exceeded its time limit."


class VerificationError(ReproGateError):
    """Raised when a verification run cannot be completed."""

    error_code = "verification_error"
    message = "The verification run failed."


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the application-wide error handlers to ``app``."""

    @app.exception_handler(ReproGateError)
    async def handle_reprogate_error(
        request: Request, exc: ReproGateError
    ) -> JSONResponse:
        logger.warning(
            "Request failed",
            extra={
                "error_code": exc.error_code,
                "status_code": exc.status_code,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.error_code, "message": exc.message}},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "Unhandled exception", extra={"path": request.url.path}
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": ReproGateError.error_code,
                    "message": ReproGateError.message,
                }
            },
        )
