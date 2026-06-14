"""Health-check endpoints — public, no auth, cheap (no DB)."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Returns 200 immediately; no dependencies checked."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness probe. Returns 200 when the app can serve traffic, 503 otherwise.

    Real DB check arrives in issue #9. For now always 200.
    Returns JSONResponse directly so a 503 structured body can be added
    later without refactoring the caller.
    """
    return JSONResponse(content={"status": "ok"})
