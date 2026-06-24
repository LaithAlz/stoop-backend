"""Stoop API — entry point.

Usage:
    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import fastapi
import structlog
import structlog.contextvars
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.integrations.supabase_auth import AuthError
from app.middleware.request_id import RequestIDMiddleware
from app.observability import configure_logging, init_sentry
from app.routers import health, me

log = structlog.get_logger(__name__)


def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    """Convert an ``AuthError`` into a 401 with the standard error envelope.

    Error envelope shape (api-contracts.md):
        {"error": {"code": "...", "message": "...", "request_id": "..."}}

    ``request_id`` is pulled from the structlog contextvar bound by
    ``RequestIDMiddleware`` — may be None if the middleware hasn't run yet
    (e.g. a test that hits the handler directly), which is acceptable.

    Security: the raw token NEVER appears in this response.  The ``message``
    is intentionally generic; only the ``code`` distinguishes error types.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            }
        },
    )


def create_app() -> fastapi.FastAPI:
    """App factory — returns a fully configured FastAPI application.

    Calling this at module level (``app = create_app()``) ensures the
    ASGI app exists at import time so uvicorn's ``app.main:app`` import
    works without any lazy initialisation.

    Startup order:
      1. configure_logging()  — structlog JSON setup
      2. init_sentry()        — no-op unless SENTRY_DSN is set
      3. add RequestIDMiddleware
      4. register AuthError exception handler (401 → standard envelope)
      5. include health router (always)
      6. include auth-test router (always — for manual JWT verification)
      7. include debug router (non-production only)
    """
    configure_logging()
    init_sentry()

    application = fastapi.FastAPI(
        title="Stoop API",
        description="AI-powered tenant-maintenance handling for landlords.",
        version="0.1.0",
    )

    application.add_middleware(RequestIDMiddleware)

    # Register the AuthError handler so any router can raise AuthError and
    # get the standard 401 envelope without boilerplate.
    application.add_exception_handler(
        AuthError,
        _auth_error_handler,  # type: ignore[arg-type]
    )

    application.include_router(health.router)
    application.include_router(me.router)

    # auth-test: always registered so engineers can verify JWT plumbing with
    # real Supabase tokens in any environment.
    from app.routers import auth_test

    application.include_router(auth_test.router)

    if not settings.is_production:
        from app.routers import debug

        application.include_router(debug.router)

    return application


app: fastapi.FastAPI = create_app()
