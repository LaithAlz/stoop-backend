"""Supabase JWT verification — JWKS-backed, asymmetric (ES256 / RS256 only).

Security properties enforced here:
- JWKS fetched asynchronously via httpx; cached for 24 h in a module-level
  dict protected by an asyncio.Lock (concurrency-safe on a single Python
  process; each Fly machine has its own cache).
- Explicit algorithm allowlist: ["ES256", "RS256"] only.  NEVER accepts
  "alg: none" or any HS* symmetric algorithm — prevents alg-confusion
  attacks (where an attacker signs HS256 using the public key as the
  secret).
- Verifies: signature, exp, iss (settings.supabase_jwt_issuer), aud
  ("authenticated").
- role claim MUST equal "authenticated" — rejects service_role tokens.
- Identity object is frozen (immutable after creation).
- NEVER logs the token, Authorization header, or any sub-string thereof.
  Only auth_user_id (the sub UUID) is logged after successful verification.

Never-break rule #5: no token, JWT sub-string, or Authorization header
value appears in any log line, exception message, or the error envelope.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import jwt
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_ALGORITHMS: list[str] = ["ES256", "RS256"]
_AUD = "authenticated"
_JWKS_TTL_SECONDS: float = 86_400.0  # 24 hours

# ---------------------------------------------------------------------------
# JWKS cache (module-level, protected by an asyncio.Lock)
# ---------------------------------------------------------------------------

# (_jwks_data, _fetched_at_monotonic)
_jwks_cache: tuple[dict[str, Any], float] | None = None
_jwks_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Custom exception — carries a stable error code for the HTTP layer
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised by the verifier on any authentication failure.

    ``code`` is a stable snake_case string used in the JSON error envelope.
    ``message`` is human-readable and intentionally generic — it must not
    reveal which specific check failed (and must never contain token material).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Frozen identity object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthUser:
    """Verified identity extracted from a Supabase JWT.

    Fields
    ------
    user_id : UUID
        The stable ``sub`` claim — FK target for ``landlords.auth_user_id``.
    email : str | None
        The ``email`` claim.  May change if the user updates their address;
        treat as display-only contact info, never as an authorization key.
    full_name : str | None
        ``user_metadata.full_name`` — **user-writable**; use only for display.
        Never use as an authorization signal.
    """

    user_id: UUID
    email: str | None
    full_name: str | None


# ---------------------------------------------------------------------------
# Internal JWKS helpers
# ---------------------------------------------------------------------------


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch the JWKS from Supabase and return the parsed dict.

    Called only when the cache is cold or expired. Runs in an async context
    using httpx (compatible with respx mocking in tests).
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            settings.supabase_jwks_url,
            timeout=10.0,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data


async def _get_jwks() -> dict[str, Any]:
    """Return the cached JWKS, fetching from Supabase if the cache is cold/expired.

    Thread-safety: asyncio.Lock ensures only one coroutine performs a fetch
    at a time; others wait then use the freshly-cached result.
    """
    global _jwks_cache  # noqa: PLW0603

    # Fast path (no lock): check if we have a fresh cache.
    if _jwks_cache is not None:
        data, fetched_at = _jwks_cache
        if time.monotonic() - fetched_at < _JWKS_TTL_SECONDS:
            return data

    # Slow path: acquire the lock, re-check (double-check locking), then fetch.
    async with _jwks_lock:
        if _jwks_cache is not None:
            data, fetched_at = _jwks_cache
            if time.monotonic() - fetched_at < _JWKS_TTL_SECONDS:
                return data

        data = await _fetch_jwks()
        _jwks_cache = (data, time.monotonic())
        return data


def _find_signing_key(jwks: dict[str, Any], kid: str) -> Any:
    """Return the PyJWK object matching ``kid`` from a JWKS dict.

    Raises AuthError (invalid_token) if no key matches.
    """
    from jwt import PyJWKSet

    try:
        jwk_set = PyJWKSet.from_dict(jwks)
    except Exception as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    for key in jwk_set.keys:
        if key.key_id == kid:
            return key.key

    raise AuthError("invalid_token", "Authentication failed.")


# ---------------------------------------------------------------------------
# Public verification function
# ---------------------------------------------------------------------------


async def verify_jwt(token: str) -> AuthUser:
    """Verify a Supabase access token and return the authenticated identity.

    Security checks (in order):
    1. Decode the unverified header to extract ``kid``.
    2. Fetch/cache JWKS and locate the signing key by ``kid``.
    3. Decode + verify using PyJWT with an explicit algorithm allowlist.
       This verifies: signature, ``exp``, ``iss``, ``aud``.
    4. Assert ``role == "authenticated"`` to reject service_role tokens.
    5. Map claims to a frozen AuthUser.

    NEVER logs the token or any substring of it.

    Raises
    ------
    AuthError
        On any verification failure.  The ``code`` field distinguishes
        ``expired`` from generic ``invalid_token`` / ``forbidden_role`` /
        ``invalid_token`` (unknown kid).
    """
    # Step 1 — extract kid from the unverified header.
    # We do NOT decode the payload here, only the header.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    kid: str | None = header.get("kid")
    if not kid:
        raise AuthError("invalid_token", "Authentication failed.")

    # Guard: reject any token that declares a non-allowlisted algorithm
    # before we even touch the key material.  PyJWT's decode() already
    # enforces the allowlist but this catches it one step earlier and makes
    # the intent explicit.
    alg: str | None = header.get("alg")
    if alg not in _ALLOWED_ALGORITHMS:
        raise AuthError("invalid_token", "Authentication failed.")

    # Step 2 — fetch/cache JWKS and find the matching signing key.
    try:
        jwks = await _get_jwks()
    except Exception as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    signing_key = _find_signing_key(jwks, kid)

    # Step 3 — full verification with explicit allowlist.
    # PyJWT verifies: signature, exp, iss, aud.
    # algorithms= locks out "none" and all HS* algorithms.
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=_AUD,
            issuer=settings.supabase_jwt_issuer,
            # PyJWT only validates a claim if it is PRESENT. Require exp/iss/aud
            # so a validly-signed token that simply omits exp can't become a
            # non-expiring credential (anything able to mint under the project
            # keys must still produce short-lived tokens). Missing → rejected.
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        # Distinct code so clients can refresh silently.
        raise AuthError("expired", "Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    # Step 4 — reject service_role tokens.
    if claims.get("role") != "authenticated":
        raise AuthError("forbidden_role", "Authentication failed.")

    # Step 5 — map claims to frozen identity.
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    email: str | None = claims.get("email")
    user_metadata: dict[str, Any] = claims.get("user_metadata") or {}
    full_name: str | None = user_metadata.get("full_name")

    identity = AuthUser(user_id=user_id, email=email, full_name=full_name)

    # Safe to log: only the UUID, never the token.
    log.info("auth_verified", auth_user_id=str(identity.user_id))

    return identity
