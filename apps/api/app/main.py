"""Stoop API — entry point.

Usage:
    uv run uvicorn app.main:app --reload
"""

import fastapi

from app.routers import health


def create_app() -> fastapi.FastAPI:
    """App factory — returns a fully configured FastAPI application.

    Calling this at module level (``app = create_app()``) ensures the
    ASGI app exists at import time so uvicorn's ``app.main:app`` import
    works without any lazy initialisation.
    """
    application = fastapi.FastAPI(
        title="Stoop API",
        description="AI-powered tenant-maintenance handling for landlords.",
        version="0.1.0",
    )

    application.include_router(health.router)

    return application


app: fastapi.FastAPI = create_app()
