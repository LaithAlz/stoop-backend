"""Debug-only endpoints — NOT registered in production.

These endpoints are included in ``create_app()`` only when
``not settings.is_production``.  They are used for verifying that
structured logging and Sentry capture work correctly in dev/staging.

Endpoints:
  GET /_debug/log   — emits a structlog line; visible on stdout as JSON.
  GET /_debug/error — raises an unhandled exception; Sentry captures it
                      when a DSN is configured.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter

log = structlog.get_logger()

router = APIRouter(prefix="/_debug", tags=["debug"])


@router.get("/log")
async def debug_log() -> dict[str, str]:
    """Emit a structured log line and return 200.

    The log line will contain ``request_id`` (bound by RequestIDMiddleware)
    plus the explicit ``check`` field.  Useful for confirming that JSON
    logging and context propagation are wired correctly.
    """
    log.info("debug_log_endpoint_called", check="structlog_ok")
    return {"status": "logged"}


@router.get("/error")
async def debug_error() -> None:
    """Raise an unhandled exception so Sentry can capture it.

    This endpoint deliberately raises ``RuntimeError``.  When a Sentry DSN
    is configured, the Sentry FastAPI integration captures the event
    automatically.  Without a DSN the exception still propagates (→ 500).
    """
    raise RuntimeError("debug_error: intentional exception for Sentry smoke-test")
