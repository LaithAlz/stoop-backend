"""Temporary auth-test endpoint — manual verification with real Supabase tokens.

``GET /v1/auth-test`` returns the authenticated identity so that engineers
can confirm the JWKS verification pipeline works end-to-end with a live
Supabase project.

This router is registered unconditionally (not gated by environment) because
auth-test tokens help debug production wiring.  It reveals nothing sensitive:
the user_id is a UUID already known to the caller (they own the token), and
we never echo the token back.

Remove this endpoint in a follow-up PR once issue #11 ships ``GET /v1/me``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import require_user
from app.integrations.supabase_auth import AuthUser

router = APIRouter(prefix="/v1", tags=["auth-test"])


@router.get("/auth-test")
async def auth_test(user: AuthUser = Depends(require_user)) -> dict[str, str | None]:  # noqa: B008
    """Return the verified identity from the Bearer token.

    200 — authenticated; body contains ``user_id``, ``email``, ``full_name``.
    401 — unauthenticated; body uses the standard error envelope.

    This endpoint never echoes the token.
    """
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "full_name": user.full_name,
    }
