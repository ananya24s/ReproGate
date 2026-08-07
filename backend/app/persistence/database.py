"""Database engine, session factory, and declarative base.

This module owns connection management only. Tables live in
``app.persistence.models`` and queries live in ``app.persistence.repositories``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every persistence model."""


_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    str(_settings.database_url),
    echo=_settings.database_echo,
    pool_size=_settings.database_pool_size,
    max_overflow=_settings.database_max_overflow,
    pool_timeout=_settings.database_pool_timeout,
    pool_recycle=_settings.database_pool_recycle,
    pool_pre_ping=True,
)

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session that is rolled back on error and always closed."""
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close every pooled connection. Called during application shutdown."""
    await engine.dispose()
