"""Request-scoped database dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""Injects a request-scoped :class:`AsyncSession` into a route handler."""
