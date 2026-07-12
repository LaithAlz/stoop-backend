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

from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import settings
from app.db.session import verify_request_engine_role_separation
from app.errors import AppError
from app.integrations.supabase_auth import AuthError
from app.middleware.request_id import RequestIDMiddleware
from app.observability import configure_logging, init_langsmith_tracing, init_sentry
from app.routers import cases, health, me, notifications, properties, tenants, vendors
from app.routers.webhooks import twilio as webhooks_twilio
from app.scheduler import start_scheduler, stop_scheduler

log = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(_app: fastapi.FastAPI) -> AsyncIterator[None]:
    """Startup self-checks (#22 safety review item 13b; #24).

    ``verify_request_engine_role_separation`` proves — not just assumes —
    that the request-path engine is genuinely isolated from the admin
    engine whenever ``APP_DATABASE_URL`` is set. Raising here (before
    ``yield``) aborts FastAPI/uvicorn startup entirely: refusing to serve
    any traffic is the correct failure mode for "RLS role separation was
    configured wrong," which is worse silently than not configuring it at
    all. See ``app/db/session.py``'s module docstring for the full
    rationale.

    ``setup_checkpointer()`` runs AFTER that check, unconditionally
    (#24) — it idempotently creates/migrates the LangGraph checkpoint
    tables in the dedicated ``langgraph`` schema (see
    ``app/agent/checkpointer.py``'s module docstring). Fail-closed: a
    failure here RAISES and aborts startup, same as the role-separation
    check above — the agent graph cannot run without checkpoint tables,
    so serving traffic with a broken checkpoint store is worse than not
    starting at all. Cheap to run on every process start even when the
    graph/Anthropic key goes unused this deploy.

    ``start_scheduler()`` runs LAST, after both checks above pass — the
    60-second ticker (``app/scheduler.py``, #108/#109) that drives the
    emergency escalation chain sweep and the degraded-mode retry sweep.
    Never raises (it only schedules an ``asyncio.Task``); shutdown
    symmetry stops it via ``stop_scheduler()`` before the checkpointer's
    pool closes, so no sweep tick is ever mid-flight against a
    just-closed connection pool.
    """
    await verify_request_engine_role_separation()
    await setup_checkpointer()
    start_scheduler()
    yield
    # Shutdown symmetry, reverse order: stop the scheduler first (no new
    # sweep ticks once this returns), then close the checkpointer's
    # dedicated psycopg pool so a graceful stop doesn't abandon open
    # sockets/worker tasks.
    await stop_scheduler()
    await close_checkpointer()


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
      1. configure_logging()        — structlog JSON setup
      2. init_sentry()              — no-op unless SENTRY_DSN is set
      2b. init_langsmith_tracing()  — no-op unless LANGSMITH_API_KEY is set
      3. add RequestIDMiddleware
      4. register AuthError exception handler (401 → standard envelope)
      4b. register AppError exception handler (status_code → standard envelope)
      5. include health router (always)
      5a. include properties/tenants/vendors/cases routers (#54/#55 —
          always, landlord-scoped via require_landlord) and the
          notifications router (always — POST /v1/notifications/{id}/ack
          is landlord-authenticated; GET /ack/{token} is the public
          tokenized-link ack surface, #108)
      5b. include Twilio webhook router (always — no auth header, its own
          signature verification; not gated by environment since Twilio
          must reach it in every deployment, including production)
      6. include auth-test router (always — for manual JWT verification)
      7. include debug router (non-production only)

    ``lifespan=_lifespan`` runs ``verify_request_engine_role_separation``,
    then ``setup_checkpointer()``, then ``start_scheduler()`` once at ASGI
    startup (#22 safety review item 13b; #24; #108/#109) — see that
    function's docstring for what each checks/starts and why. The
    role-separation check is a no-op when ``APP_DATABASE_URL`` is unset;
    checkpoint setup and the scheduler always run.
    """
    configure_logging()
    init_sentry()
    init_langsmith_tracing()

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
    application.include_router(properties.router)
    application.include_router(tenants.router)
    application.include_router(vendors.router)
    application.include_router(cases.router)
    application.include_router(notifications.router)
    application.include_router(webhooks_twilio.router)

    # auth-test: always registered so engineers can verify JWT plumbing with
    # real Supabase tokens in any environment.
    from app.routers import auth_test

    application.include_router(auth_test.router)

    if not settings.is_production:
        from app.routers import debug

        application.include_router(debug.router)

    return application


app: fastapi.FastAPI = create_app()
