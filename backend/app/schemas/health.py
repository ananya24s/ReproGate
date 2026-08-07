"""Response models for service health endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class HealthStatus(BaseModel):
    """Liveness response for load balancers and container health checks."""

    status: str
    service: str
    environment: str
