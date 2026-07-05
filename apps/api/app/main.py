"""Stoop API — entry point.

Usage:
    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fastapi
import structlog
import structlog.contextvars
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.session import verify_request_engine_role_separation
from app.errors import AppError
from app.integrations.supabase_auth import AuthError
from app.middleware.request_id import RequestIDMiddleware
from app.observability import configure_logging, init_sentry
from app.routers import health, me

log = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(_app: fastapi.FastAPI) -> AsyncIterator[None]:
    """Startup self-check (#22 safety review item 13b).

    ``verify_request_engine_role_separation`` proves — not just assumes —
    that the request-path engine is genuinely isolated from the admin
    engine whenever ``APP_DATABASE_URL`` is set. Raising here (before
    ``yield``) aborts FastAPI/uvicorn startup entirely: refusing to serve
    any traffic is the correct failure mode for "RLS role separation was
    configured wrong," which is worse silently than not configuring it at
    all. See ``app/db/session.py``'s module docstring for the full
    rationale.
    """
    await verify_request_engine_role_separation()
    yield


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


def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Convert an ``AppError`` into its declared status code + the standard envelope.

    Same envelope shape and ``request_id`` sourcing as ``_auth_error_handler``,
    but for business-rule (non-auth) failures that need a status other than
    401 — e.g. 403 ``email_required`` on ``GET /v1/me``.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=exc.status_code,
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
      4b. register AppError exception handler (status_code → standard envelope)
      5. include health router (always)
      6. include auth-test router (always — for manual JWT verification)
      7. include debug router (non-production only)

    ``lifespan=_lifespan`` runs ``verify_request_engine_role_separation``
    once at ASGI startup (#22 safety review item 13b) — see that
    function's docstring (``app/db/session.py``) for what it checks and
    why. A no-op when ``APP_DATABASE_URL`` is unset.
    """
    configure_logging()
    init_sentry()

    application = fastapi.FastAPI(
        title="Stoop API",
        description="AI-powered tenant-maintenance handling for landlords.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    application.add_middleware(RequestIDMiddleware)

    # Register the AuthError handler so any router can raise AuthError and
    # get the standard 401 envelope without boilerplate.
    application.add_exception_handler(
        AuthError,
        _auth_error_handler,  # type: ignore[arg-type]
    )

    # Register the AppError handler so any router can raise AppError with an
    # arbitrary status code and get the standard envelope without boilerplate.
    application.add_exception_handler(
        AppError,
        _app_error_handler,  # type: ignore[arg-type]
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
