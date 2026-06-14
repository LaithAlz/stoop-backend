"""Stoop API — entry point.

Usage:
    uv run uvicorn app.main:app --reload
"""

import fastapi

from app.config import settings
from app.middleware.request_id import RequestIDMiddleware
from app.observability import configure_logging, init_sentry
from app.routers import health


def create_app() -> fastapi.FastAPI:
    """App factory — returns a fully configured FastAPI application.

    Calling this at module level (``app = create_app()``) ensures the
    ASGI app exists at import time so uvicorn's ``app.main:app`` import
    works without any lazy initialisation.

    Startup order:
      1. configure_logging()  — structlog JSON setup
      2. init_sentry()        — no-op unless SENTRY_DSN is set
      3. add RequestIDMiddleware
      4. include health router (always)
      5. include debug router (non-production only)
    """
    configure_logging()
    init_sentry()

    application = fastapi.FastAPI(
        title="Stoop API",
        description="AI-powered tenant-maintenance handling for landlords.",
        version="0.1.0",
    )

    application.add_middleware(RequestIDMiddleware)

    application.include_router(health.router)

    if not settings.is_production:
        from app.routers import debug

        application.include_router(debug.router)

    return application


app: fastapi.FastAPI = create_app()
