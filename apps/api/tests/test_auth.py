"""Tests for Supabase JWT verification (issue #10).

All tests are pure unit tests — no database, no real Supabase.  The JWKS
endpoint is mocked with respx (intercepting httpx calls made by
supabase_auth._fetch_jwks).

Harness summary
---------------
- Generate an EC P-256 keypair in-fixture (cryptography lib).
- Expose the public key as a JWKS JSON served by a respx route.
- Mint tokens with PyJWT signed by the private key (ES256).
- Each test exercises a single security property.

Security properties tested
--------------------------
1  valid token → 200 + correct identity
2  missing Authorization header → 401 missing_token
3  malformed Authorization (no "Bearer ") → 401 missing_token
4  bad signature (different key) → 401 invalid_token
5  expired token → 401 expired
6  wrong iss → 401 invalid_token
7  wrong aud → 401 invalid_token
8  role: service_role → 401 forbidden_role
9  alg: none token → 401 invalid_token
10 alg-confusion: HS256 signed with PEM public key → 401 invalid_token
11 unknown kid (not in JWKS) → 401 invalid_token
12 JWKS cache: second verify does NOT re-fetch (respx route called once)
13 /v1/auth-test valid token → 200 + identity shape
14 /v1/auth-test no token → 401 + standard error envelope shape
15 token string NEVER appears in caplog / structlog output on failure
16 omitted-exp token → 401 invalid_token (exp is required, not optional)
17 valid token without user_metadata → full_name is None
18 kid miss after key rotation → forced refresh picks up the new key, succeeds
19 kid miss DoS guard → repeated unknown-kid verifies refetch at most once
20 kid miss rate-limit window expiry → a second forced refresh is allowed
   once the window has passed (issue #134)
21 kid miss + failed forced refresh (500) still consumes the rate-limit
   window — an attacker can't drive one upstream attempt per request while
   Supabase's JWKS endpoint is erroring (issue #134 safety review, B1)
22 kid miss + degenerate empty-keys 200 response does NOT clobber a
   known-good cache (issue #134 safety review, A1)
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import respx
import structlog.testing
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import ASGITransport

import app.integrations.supabase_auth as auth_mod
from app.integrations.supabase_auth import AuthError, AuthUser, verify_jwt
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-001"


def _b64url(n: int) -> str:
    """Encode an integer as an unpadded url-safe base64 string (for JWK coords)."""
    byte_length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()


def _make_keypair() -> tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]:
    """Generate an EC P-256 keypair."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key()
    return private, public


def _public_key_to_jwks(public: EllipticCurvePublicKey, kid: str) -> dict[str, Any]:
    """Build a JWKS dict from an EC public key."""
    nums = public.public_numbers()
    return {
        "keys": [
            {
                "kty": "EC",
                "crv": "P-256",
                "x": _b64url(nums.x),
                "y": _b64url(nums.y),
                "kid": kid,
                "use": "sig",
                "alg": "ES256",
            }
        ]
    }


def _mint_token(
    private: EllipticCurvePrivateKey,
    *,
    kid: str = _KID,
    alg: str = "ES256",
    sub: str = "11111111-1111-1111-1111-111111111111",
    iss: str = _ISSUER,
    aud: str = "authenticated",
    role: str = "authenticated",
    email: str = "alice@example.com",
    full_name: str | None = "Alice Landlord",
    exp_offset: int | None = 3600,
) -> str:
    """Mint a JWT signed with the given private key and claims.

    ``exp_offset=None`` omits the ``exp`` claim entirely (to exercise the
    require-exp guard).
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "role": role,
        "email": email,
        "iat": now,
    }
    if exp_offset is not None:
        payload["exp"] = now + exp_offset
    if full_name is not None:
        payload["user_metadata"] = {"full_name": full_name}

    return jwt.encode(payload, private, algorithm=alg, headers={"kid": kid})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def keypair() -> tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]:
    return _make_keypair()


@pytest.fixture()
def private_key(
    keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey],
) -> EllipticCurvePrivateKey:
    return keypair[0]


@pytest.fixture()
def public_key(
    keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey],
) -> EllipticCurvePublicKey:
    return keypair[1]


@pytest.fixture()
def jwks_payload(public_key: EllipticCurvePublicKey) -> dict[str, Any]:
    return _public_key_to_jwks(public_key, _KID)


@pytest.fixture(autouse=True)
def reset_jwks_cache() -> None:
    """Reset the module-level JWKS cache before each test."""
    auth_mod._jwks_cache = None  # noqa: SLF001


# ---------------------------------------------------------------------------
# respx context manager: serve the real JWKS
# ---------------------------------------------------------------------------


def _make_jwks_router(jwks: dict[str, Any]) -> respx.MockRouter:
    """Return a respx router that serves the given JWKS JSON."""
    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    return router


# ---------------------------------------------------------------------------
# Test 1 — valid token → correct identity
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_valid_token_returns_identity(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key)

    async with _make_jwks_router(jwks_payload):
        user = await verify_jwt(token)

    assert isinstance(user, AuthUser)
    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert user.email == "alice@example.com"
    assert user.full_name == "Alice Landlord"


@pytest.mark.unit
async def test_valid_token_without_user_metadata_yields_none_full_name(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """A token with no user_metadata → full_name is None (it's optional)."""
    token = _mint_token(private_key, full_name=None)

    async with _make_jwks_router(jwks_payload):
        user = await verify_jwt(token)

    assert user.full_name is None
    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Test 2 — missing Authorization header → 401 missing_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_auth_header_raises_missing_token() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth-test")

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "missing_token"


# ---------------------------------------------------------------------------
# Test 3 — malformed Authorization (no "Bearer ")
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_malformed_auth_header_raises_missing_token() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth-test", headers={"Authorization": "Token some-value"})

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "missing_token"


# ---------------------------------------------------------------------------
# Test 4 — bad signature (different key) → 401 invalid_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_bad_signature_raises_invalid_token(
    jwks_payload: dict[str, Any],
) -> None:
    # Mint a token with a *different* private key — signature won't verify.
    other_private, _ = _make_keypair()
    token = _mint_token(other_private)

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 5 — expired token → 401 expired
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_expired_token_raises_expired(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key, exp_offset=-1)  # expired 1s ago

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "expired"


@pytest.mark.unit
async def test_token_without_exp_is_rejected(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """A validly-signed token that OMITS exp must be rejected, not accepted
    as a non-expiring credential (PyJWT only checks exp if present; we require
    it). See never-break rule #5 / short-lived-token model."""
    token = _mint_token(private_key, exp_offset=None)  # no exp claim at all

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 6 — wrong iss → 401 invalid_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_wrong_iss_raises_invalid_token(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key, iss="https://evil.example.com/auth/v1")

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 7 — wrong aud → 401 invalid_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_wrong_aud_raises_invalid_token(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key, aud="service_role")

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 8 — role: service_role → 401 forbidden_role
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_service_role_token_raises_forbidden_role(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    # aud is still "authenticated" but role is "service_role"
    token = _mint_token(private_key, role="service_role")

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "forbidden_role"


# ---------------------------------------------------------------------------
# Test 9 — alg: none token → 401 invalid_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_alg_none_token_raises_invalid_token(
    jwks_payload: dict[str, Any],
) -> None:
    # Build a header with alg: none manually.
    # PyJWT doesn't support encoding "none" directly, so we craft the raw JWT.
    import base64 as _b64

    header = _b64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT", "kid": _KID}).encode()
    ).rstrip(b"=")
    now = int(time.time())
    payload_dict = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "iss": _ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": now + 3600,
    }
    payload_b64 = _b64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=")
    # "none" algorithm: signature is empty
    token = header.decode() + "." + payload_b64.decode() + "."

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 10 — alg-confusion: HS256 signed with PEM public key → 401
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_alg_confusion_hs256_with_public_key_raises_invalid_token(
    public_key: EllipticCurvePublicKey,
    jwks_payload: dict[str, Any],
) -> None:
    """Attacker signs a HS256 token using the PEM-encoded public key as secret.

    The allowlist must reject HS256 before the key even gets looked up.

    Modern PyJWT refuses to encode HS256 with a PEM key, so we craft the
    token manually using the hmac module — exactly as a malicious client
    using a less-strict library would.
    """
    import base64 as _b64
    import hashlib
    import hmac as _hmac

    pem_public = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    now = int(time.time())
    header_b64 = _b64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT", "kid": _KID}).encode()
    ).rstrip(b"=")
    payload_dict: dict[str, Any] = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "iss": _ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": now + 3600,
    }
    payload_b64 = _b64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=")

    signing_input = header_b64 + b"." + payload_b64
    sig = _hmac.new(pem_public, signing_input, hashlib.sha256).digest()
    sig_b64 = _b64.urlsafe_b64encode(sig).rstrip(b"=")
    token = signing_input.decode() + "." + sig_b64.decode()

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 11 — unknown kid → 401 invalid_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unknown_kid_raises_invalid_token(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key, kid="unknown-kid-xyz")

    async with _make_jwks_router(jwks_payload):
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)

    assert exc_info.value.code == "invalid_token"


# ---------------------------------------------------------------------------
# Test 12 — JWKS cache: second verify does NOT re-fetch
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_jwks_cache_single_fetch(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """After the first verify populates the cache, a second verify must NOT
    re-fetch the JWKS.  We assert the respx route was called exactly once
    across two successful verifications — proving the 24 h cache is working.
    """
    token = _mint_token(private_key)

    async with _make_jwks_router(jwks_payload) as mock_router:
        await verify_jwt(token)
        await verify_jwt(token)  # second call — must use cache
        call_count = mock_router.calls.call_count

    # The JWKS URL should have been fetched exactly once.
    assert call_count == 1, f"JWKS fetched {call_count} times — cache is not working"


# ---------------------------------------------------------------------------
# Test 13 — /v1/auth-test with valid token → 200 + identity
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_auth_test_endpoint_valid_token(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    token = _mint_token(private_key)

    async with _make_jwks_router(jwks_payload):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/v1/auth-test",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["email"] == "alice@example.com"
    assert body["full_name"] == "Alice Landlord"


# ---------------------------------------------------------------------------
# Test 14 — /v1/auth-test without token → 401 + standard error envelope
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_auth_test_endpoint_no_token_returns_standard_envelope() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/auth-test")

    assert response.status_code == 401
    body = response.json()
    # Standard error envelope shape: {"error": {"code", "message", "request_id"}}
    assert "error" in body
    error = body["error"]
    assert "code" in error
    assert "message" in error
    assert "request_id" in error
    assert error["code"] == "missing_token"


# ---------------------------------------------------------------------------
# Test 15 — token string never appears in log output on failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_token_never_logged_on_failure(
    jwks_payload: dict[str, Any],
) -> None:
    """When verify_jwt fails, the raw token must not appear in any log output.

    Uses structlog.testing.capture_logs() to intercept all structlog events
    emitted during verification of a bad-signature token.
    """
    other_private, _ = _make_keypair()
    token = _mint_token(other_private)  # bad sig

    log_entries: list[dict[str, Any]] = []

    with structlog.testing.capture_logs() as cap:
        async with _make_jwks_router(jwks_payload):
            with pytest.raises(AuthError):
                await verify_jwt(token)
        log_entries = list(cap)

    # Assert token string is absent from every log entry
    all_log_text = json.dumps(log_entries)
    assert token not in all_log_text, "Raw JWT token string leaked into structured log output"

    # Also assert no partial sub-string of the token payload section appears
    token_parts = token.split(".")
    if len(token_parts) >= 2:
        # The payload section is the most sensitive — check it's not in logs
        assert token_parts[1] not in all_log_text, (
            "JWT payload section leaked into structured log output"
        )


# ---------------------------------------------------------------------------
# Test 18 — kid miss after key rotation → forced refresh succeeds (#134)
# ---------------------------------------------------------------------------
#
# NOTE on respx usage in this section: per issue #145, we never nest respx
# contexts (that raced on a cold JWKS cache and caused a flake). Each test
# below uses a single respx.MockRouter as an async context manager, with a
# ``side_effect`` list of canned responses consumed in call order — this lets
# us simulate "JWKS changes between fetches" (a rotation) without more than
# one respx context per test.


@pytest.mark.unit
async def test_kid_miss_forces_refresh_and_succeeds_after_rotation(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """Simulates a Supabase signing-key rotation.

    The cache is primed by verifying a token signed with the original key
    (``kid`` = ``_KID``). A second token, signed by a brand-new key with a
    *different* ``kid``, is then verified — the first ``_find_signing_key``
    lookup misses (stale cache), which must force exactly one JWKS refresh
    before retrying; the refreshed JWKS contains the new key, so the second
    verify succeeds.
    """
    token_original = _mint_token(private_key, kid=_KID)

    new_private, new_public = _make_keypair()
    new_kid = "test-kid-rotated"
    rotated_jwks = _public_key_to_jwks(new_public, new_kid)
    token_rotated = _mint_token(new_private, kid=new_kid)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # prime: serves original key
            httpx.Response(200, json=rotated_jwks),  # forced refresh: serves rotated key
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache
        user = await verify_jwt(token_rotated)  # kid miss -> forced refresh -> success
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 2, (
        f"expected exactly one prime fetch + one forced refresh, got {call_count}"
    )


# ---------------------------------------------------------------------------
# Test 19 — DoS guard: repeated unknown-kid verifies refetch at most once (#134)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_repeated_unknown_kid_does_not_refetch_more_than_once_per_window(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """An attacker spamming unknown ``kid`` values must not drive unbounded
    refetches against Supabase's JWKS endpoint.

    The route is only wired with two canned responses (one prime + one
    forced refresh); if the implementation refetched more than once per
    rate-limit window, respx would raise on the exhausted side_effect
    iterator instead of this test's own assertion — a built-in tripwire.
    """
    token_original = _mint_token(private_key, kid=_KID)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # prime
            httpx.Response(200, json=jwks_payload),  # single forced refresh (kid still absent)
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache

        for i in range(5):
            bad_token = _mint_token(private_key, kid=f"unknown-kid-{i}")
            with pytest.raises(AuthError) as exc_info:
                await verify_jwt(bad_token)
            assert exc_info.value.code == "invalid_token"

        call_count = route.call_count

    assert call_count == 2, (
        f"JWKS fetched {call_count} times across 5 unknown-kid verifies "
        "— forced-refresh rate limit not enforced"
    )


# ---------------------------------------------------------------------------
# Test 20 — rate-limit window expiry allows a new forced refresh (#134)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_forced_refresh_allowed_again_after_window_expires(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second forced refresh IS allowed once the rate-limit window has
    passed — using the ``_now`` monkeypatch seam, never a real ``sleep()``.
    """
    token_original = _mint_token(private_key, kid=_KID)
    token_still_unknown = _mint_token(private_key, kid="unknown-kid-during-window")

    new_private, new_public = _make_keypair()
    new_kid = "test-kid-rotated-later"
    rotated_jwks = _public_key_to_jwks(new_public, new_kid)
    token_rotated = _mint_token(new_private, kid=new_kid)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # fetch #1: prime
            httpx.Response(200, json=jwks_payload),  # fetch #2: forced refresh, still no match
            httpx.Response(200, json=rotated_jwks),  # fetch #3: forced refresh after window
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1

        with pytest.raises(AuthError):
            await verify_jwt(token_still_unknown)  # fetch #2 (forced refresh, still a miss)

        assert auth_mod._last_forced_refresh is not None
        stale_stamp = auth_mod._last_forced_refresh

        # Advance the clock past the rate-limit window using the _now seam
        # (never sleep()).
        monkeypatch.setattr(
            auth_mod, "_now", lambda: stale_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0
        )

        user = await verify_jwt(token_rotated)  # fetch #3: window passed, refresh allowed
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 3, (
        f"expected prime + rate-limited-miss + post-window refresh (3 fetches), got {call_count}"
    )


# ---------------------------------------------------------------------------
# Test 21 — failed forced refresh (5xx) still consumes the rate-limit window
# (issue #134 safety review, B1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_failed_forced_refresh_still_consumes_rate_limit_window(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced refresh attempt that itself fails (upstream 5xx) must still
    consume the rate-limit window.

    The window bounds *attempts*, not successes: otherwise an attacker
    spamming unknown ``kid`` values while Supabase's JWKS endpoint happens
    to be erroring could drive one upstream fetch attempt per request,
    hammering the endpoint exactly while it's recovering.
    """
    token_original = _mint_token(private_key, kid=_KID)
    unknown_token_1 = _mint_token(private_key, kid="unknown-kid-during-outage-1")
    unknown_token_2 = _mint_token(private_key, kid="unknown-kid-during-outage-2")

    new_private, new_public = _make_keypair()
    new_kid = "test-kid-after-outage"
    rotated_jwks = _public_key_to_jwks(new_public, new_kid)
    token_after_outage = _mint_token(new_private, kid=new_kid)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # fetch #1: prime
            httpx.Response(500),  # fetch #2: forced refresh attempt fails (upstream 5xx)
            httpx.Response(200, json=rotated_jwks),  # fetch #3: after window, succeeds
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache

        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(unknown_token_1)  # fetch #2: forced refresh attempt, 500 -> fails
        assert exc_info.value.code == "invalid_token"

        # A second unknown-kid verify, still within the window: must be
        # rate-limited (no new fetch attempt) even though the first
        # attempt failed rather than succeeded.
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(unknown_token_2)
        assert exc_info.value.code == "invalid_token"

        call_count_within_window = route.call_count
        assert auth_mod._last_forced_refresh is not None
        stale_stamp = auth_mod._last_forced_refresh

        # Advance the clock past the rate-limit window (never sleep()):
        # a new attempt is allowed again, and this one succeeds.
        monkeypatch.setattr(
            auth_mod, "_now", lambda: stale_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0
        )

        user = await verify_jwt(token_after_outage)  # fetch #3
        call_count_after_window = route.call_count

    assert call_count_within_window == 2, (
        "expected exactly 2 fetches (prime + one failed attempt) within the "
        f"rate-limit window, got {call_count_within_window}"
    )
    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count_after_window == 3


# ---------------------------------------------------------------------------
# Test 22 — degenerate empty-keys 200 response does not clobber a known-good
# cache (issue #134 safety review, A1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_degenerate_empty_keys_response_does_not_clobber_cache(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """A forced refresh that returns HTTP 200 with a degenerate, empty
    ``{"keys": []}`` body must NOT clobber the existing known-good cache.

    Otherwise a single bad-but-200 upstream response would fail closed ALL
    dashboard auth for up to the 24h TTL — worse than doing nothing.
    """
    token_original = _mint_token(private_key, kid=_KID)
    unknown_token = _mint_token(private_key, kid="unknown-kid-during-glitch")

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # fetch #1: prime (key A present)
            httpx.Response(200, json={"keys": []}),  # fetch #2: forced refresh, degenerate body
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache with key A

        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(unknown_token)  # fetch #2: forced refresh, empty-keys body
        assert exc_info.value.code == "invalid_token"

        # The ORIGINAL kid must still verify successfully: the degenerate
        # empty-keys response must not have clobbered the cache, and no
        # further fetch should be attempted (cache is intact).
        user = await verify_jwt(token_original)
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 2, (
        f"expected exactly 2 fetches (prime + one degenerate attempt), got {call_count}"
    )
