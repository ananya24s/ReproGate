"""Route modules and the aggregate API router."""

from fastapi import APIRouter

from app.api.routes import health, issues, verification_runs

api_router = APIRouter()
api_router.include_router(issues.router)
api_router.include_router(verification_runs.router)

__all__ = ["api_router", "health", "issues", "verification_runs"]
