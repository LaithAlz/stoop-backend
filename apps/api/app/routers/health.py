"""Health-check endpoints — public, no auth, cheap (no DB for liveness)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.session import engine

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Returns 200 immediately; no dependencies checked."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe. Returns 200 when the DB is reachable, 503 otherwise.

    Uses a lightweight raw connection (``engine.connect()``) and executes
    ``SELECT 1`` — this does NOT query any application table, so it is
    decoupled from schema state.

    On failure the response body is deliberately generic: credentials and the
    database URL are never echoed back (never-break rule #5).  The real error
    is logged server-side via structlog at WARNING level.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        # Log enough to diagnose (exception type + message) but never the
        # DATABASE_URL or credentials.  ``str(exc)`` from asyncpg may contain
        # the DSN in some error paths, so we log only the exception *type*.
        log.warning(
            "readiness_check_failed",
            exc_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": "database not reachable"},
        )

    return JSONResponse(content={"status": "ok"})
