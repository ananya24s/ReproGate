"""Route modules and the aggregate API router."""

from fastapi import APIRouter

from app.api.routes import health, verification_runs

api_router = APIRouter()
api_router.include_router(verification_runs.router)

__all__ = ["api_router", "health", "verification_runs"]
