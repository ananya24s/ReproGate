"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router, health
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.github.client import GitHubClient
from app.persistence.database import dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage resources that live as long as the application process."""
    settings: Settings = app.state.settings
    logger.info(
        "Application starting",
        extra={"environment": settings.environment, "version": app.version},
    )

    app.state.github_client = GitHubClient(settings)
    try:
        yield
    finally:
        await app.state.github_client.aclose()
        await dispose_engine()
        logger.info("Application stopped")


def create_app() -> FastAPI:
    """Build and configure the ReproGate FastAPI application."""
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        summary="Evidence-first verification platform for AI coding agents.",
        debug=settings.debug,
        lifespan=lifespan,
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
