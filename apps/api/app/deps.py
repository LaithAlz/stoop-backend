"""FastAPI dependency providers.

``require_user`` is the gatekeeper for every authenticated endpoint.
Inject it with ``Depends(require_user)`` to receive a verified ``AuthUser``.

Usage::

    from app.deps import require_user
    from app.integrations.supabase_auth import AuthUser

    @router.get("/v1/example")
    async def example(user: AuthUser = Depends(require_user)) -> dict:
        return {"user_id": str(user.user_id)}

Security:
- Parses the ``Authorization: Bearer <token>`` header.
- Delegates full JWT verification to ``supabase_auth.verify_jwt``.
- Any failure raises ``AuthError``; the registered exception handler
  converts it to a 401 with the standard error envelope.
- The raw token is NEVER logged, stored, or echoed in error messages.
"""

from __future__ import annotations

from fastapi import Request

from app.integrations.supabase_auth import AuthError, AuthUser, verify_jwt


async def require_user(request: Request) -> AuthUser:
    """Extract and verify the Supabase JWT from the ``Authorization`` header.

    Returns the verified ``AuthUser`` on success.

    Raises
    ------
    AuthError
        ``missing_token``  — no ``Authorization`` header, or not ``Bearer …``.
        ``invalid_token``  — bad signature, wrong iss/aud/alg/kid.
        ``expired``        — token was valid but has expired.
        ``forbidden_role`` — token is valid but has ``role: service_role``.
    """
    auth_header: str | None = request.headers.get("Authorization")

    if not auth_header:
        raise AuthError("missing_token", "Authentication required.")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():  # noqa: S105
        raise AuthError("missing_token", "Authentication required.")

    token = parts[1].strip()

    # verify_jwt raises AuthError on any failure — let it propagate.
    return await verify_jwt(token)
