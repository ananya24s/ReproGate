"""Reusable FastAPI dependencies for the API layer."""

from app.api.dependencies.database import SessionDep

__all__ = ["SessionDep"]
