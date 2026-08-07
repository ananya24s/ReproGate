"""Service health endpoints used by orchestrators and container health checks."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.health import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus, summary="Liveness probe")
async def health() -> HealthStatus:
    """Report that the process is running and able to serve requests."""
    settings = get_settings()
    return HealthStatus(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
    )
