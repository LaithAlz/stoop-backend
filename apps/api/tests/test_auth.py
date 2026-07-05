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
23 EC coordinate with a leading zero byte still verifies (regression for a
   latent ``_b64url`` fixed-width encoding bug found via issue #145)
24 cold cache + degenerate {"keys": []} 200 on the routine _get_jwks path
   fails closed WITHOUT caching the empty result; a later verify with real
   keys queued triggers a fresh fetch rather than reusing the empty cache
   (issue #147, guard parity with _refresh_jwks_on_kid_miss)
25 warm cache + TTL-expired refetch that returns a degenerate
   {"keys": []} keeps serving the stale-but-real cached keys (instead of
   caching the empty body), and does NOT refresh ``fetched_at`` — a
   subsequent call still retries the fetch rather than getting a fresh
   24h TTL on the empty result (issue #147)
26 degenerate-fetch cooldown bounds a sustained-outage storm: repeated
   verifies with a cold/degenerate cache inside the cooldown window drive
   at most one routine fetch (plus at most one rate-limited kid-miss
   fetch); after the window passes, the next verify fetches again
   (issue #147 follow-up, safety review)
27 recovery clears the degenerate-fetch cooldown stamp: a good fetch after
   an incident succeeds and immediately re-arms a FRESH cooldown on the
   next degenerate incident, proven behaviorally (not just via the global)
   (issue #147 follow-up)
28 cold cache, degenerate routine fetch, degenerate kid-miss forced
   refresh, then a second verify within both rate-limit windows → hits the
   previously-"unreachable" rate-limited fallback in
   _refresh_jwks_on_kid_miss, fails closed, drives NO further fetches
   (issue #147 follow-up, spec-guardian finding)
29 a body of {} (the "keys" field entirely absent) on the routine
   _get_jwks path is treated exactly like {"keys": []} — not cached
   (issue #147 follow-up)
30 a body of {} (the "keys" field entirely absent) on the
   _refresh_jwks_on_kid_miss path is treated exactly like {"keys": []} —
   does not clobber a known-good cache (issue #147 follow-up)
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


# P-256 coordinates are always 32 bytes. Per RFC 7518 §6.2.1, the "x"/"y"
# octet strings MUST be the curve's FULL fixed width, with any leading zero
# octets preserved — NOT the minimal encoding of the integer's bit length.
_P256_COORD_BYTES = 32


def _b64url(n: int, length: int) -> str:
    """Encode an integer as a fixed-width, unpadded url-safe base64 string.

    ``length`` must be the curve's coordinate byte width (e.g. 32 for
    P-256) so that a coordinate whose leading byte happens to be zero is
    still encoded at the correct width instead of being silently
    shortened — a shortened encoding fails JWK parsing (``PyJWKSet``)
    despite the ``kid`` matching.
    """
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


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
                "x": _b64url(nums.x, _P256_COORD_BYTES),
                "y": _b64url(nums.y, _P256_COORD_BYTES),
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


# ---------------------------------------------------------------------------
# Test 23 — EC coordinate with a leading zero byte still verifies
#
# Regression test for a latent ``_b64url`` encoding bug found while
# diagnosing issue #145's residual flake. Per RFC 7518 §6.2.1, a JWK EC
# coordinate's octet string MUST be the curve's full FIXED width (32 bytes
# for P-256), with any leading zero octets preserved. The old ``_b64url``
# derived the byte length from ``n.bit_length()``, which silently produced
# a one-byte-short encoding whenever a coordinate's top byte happened to be
# zero (~0.78% of random P-256 keypairs: ``1 - (255/256)**2``) —
# ``PyJWKSet.from_dict`` then failed to parse that key and ``verify_jwt``
# raised ``invalid_token`` even though the ``kid`` matched textually.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_leading_zero_byte_coordinate_still_verifies() -> None:
    """A keypair whose ``x`` or ``y`` has a leading zero byte must still
    verify successfully via the standard path.

    Generates keypairs until one lands below ``2**248`` (i.e. its top byte,
    bits 248-255, is zero) for ``x`` or ``y``, bounded at 2000 attempts so
    the test can never hang — at ~0.78% per keypair, the odds of exhausting
    2000 independent attempts without a hit are astronomically small
    (~(0.9922)**2000, well under 1e-6), so a skip should never actually
    trigger in practice; it exists purely as a non-flaky escape hatch.
    """
    max_attempts = 2000
    leading_zero_threshold = 2**248  # below this, the top byte is 0x00

    found: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey] | None = None
    attempts_used = max_attempts
    for attempt in range(1, max_attempts + 1):
        candidate = _make_keypair()
        nums = candidate[1].public_numbers()
        if nums.x < leading_zero_threshold or nums.y < leading_zero_threshold:
            found = candidate
            attempts_used = attempt
            break

    if found is None:
        pytest.skip(f"could not generate a leading-zero-byte coordinate in {max_attempts} attempts")

    private, public = found
    jwks = _public_key_to_jwks(public, _KID)
    token = _mint_token(private)

    async with _make_jwks_router(jwks):
        user = await verify_jwt(token)

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111"), (
        f"found a leading-zero-byte coordinate after {attempts_used} attempt(s) "
        "but verification did not return the expected identity"
    )


# ---------------------------------------------------------------------------
# Test 24 — cold cache + degenerate {"keys": []} on the routine path fails
# closed WITHOUT being cached for 24h (issue #147)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_cold_cache_degenerate_keys_fails_closed_without_caching(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degenerate ``{"keys": []}`` 200 fetched by the routine ``_get_jwks``
    path (cold cache) must NOT be cached — same guard as
    ``_refresh_jwks_on_kid_miss``, applied for parity (issue #147).

    With an empty JWKS, ``_find_signing_key`` always misses (there is no
    ``kid`` to find), which triggers the existing forced-refresh-on-kid-miss
    path as an unavoidable side effect — so this first, failing ``verify_jwt``
    call consumes two fetches (the routine one and the forced-refresh one),
    both degenerate here. What this test actually proves is the second
    ``verify_jwt`` call: with a real JWKS now queued, it must trigger ANOTHER
    fetch through the routine path rather than being served the empty result
    — i.e. the degenerate body from the first call was never cached.

    The degenerate-fetch cooldown added alongside this guard (issue #147
    follow-up) would otherwise skip that second fetch entirely within its
    window — this test isn't about the cooldown itself (see
    ``test_degenerate_fetch_cooldown_bounds_repeated_verifies``), so the
    clock is advanced past the cooldown window before the second call.
    """
    token = _mint_token(private_key)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": []}),  # fetch #1: _get_jwks, cold cache
            httpx.Response(200, json={"keys": []}),  # fetch #2: forced refresh (still empty)
            httpx.Response(200, json=jwks_payload),  # fetch #3: _get_jwks retries, succeeds
        ]
    )

    async with router:
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)
        assert exc_info.value.code == "invalid_token"

        # Cache must still be cold — nothing was cached from the degenerate
        # attempts above.
        assert auth_mod._jwks_cache is None  # noqa: SLF001

        # Advance past the degenerate-fetch cooldown window so the second
        # call's routine fetch isn't skipped by the cooldown (a separate
        # mechanism from the no-cache guard under test here).
        assert auth_mod._last_degenerate_fetch is not None  # noqa: SLF001
        stale_stamp = auth_mod._last_degenerate_fetch  # noqa: SLF001
        monkeypatch.setattr(
            auth_mod, "_now", lambda: stale_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0
        )

        user = await verify_jwt(token)  # fetch #3: real keys, no longer degenerate
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 3, (
        f"expected 3 fetches (empty routine fetch + empty forced-refresh "
        f"fetch + a retried routine fetch), got {call_count} — the empty "
        "payload must not have been cached for the 24h TTL"
    )


# ---------------------------------------------------------------------------
# Test 25 — warm cache + TTL-expired refetch that returns degenerate
# {"keys": []} keeps the stale-but-real cache and does not touch fetched_at
# (issue #147)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_warm_cache_ttl_expired_degenerate_refetch_keeps_stale_cache(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm cache whose 24h TTL has expired, refetched via the routine
    ``_get_jwks`` path, may get back a degenerate ``{"keys": []}`` body.

    That must NOT clobber the existing (stale but real) cache — a stale
    real key set beats an empty one — so a token signed by the originally
    cached key must still verify. Also: ``fetched_at`` must NOT be bumped
    by the degenerate attempt, so the very next call retries the fetch
    again instead of getting a fresh 24h TTL on nothing.

    The third call's clock advance must clear BOTH the TTL (relative to
    the original fetch) AND the degenerate-fetch cooldown (relative to the
    second, degenerate fetch's own stamp) — otherwise the cooldown added
    alongside this guard (issue #147 follow-up) would itself skip the
    third fetch, which is exactly what this test needs to happen to prove
    ``fetched_at`` wasn't refreshed.
    """
    token_original = _mint_token(private_key, kid=_KID)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # fetch #1: prime, warm cache
            httpx.Response(200, json={"keys": []}),  # fetch #2: TTL-expired refetch, degenerate
            httpx.Response(200, json=jwks_payload),  # fetch #3: retried again, real keys
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache

        assert auth_mod._jwks_cache is not None  # noqa: SLF001
        original_fetched_at = auth_mod._jwks_cache[1]  # noqa: SLF001

        # Advance the clock past the 24h TTL (never sleep()) so the routine
        # path considers the cache expired and refetches.
        monkeypatch.setattr(
            auth_mod,
            "_now",
            lambda: original_fetched_at + auth_mod._JWKS_TTL_SECONDS + 1.0,
        )

        # fetch #2 is degenerate; the stale-but-real cache must be kept and
        # served, so the ORIGINALLY cached kid still verifies.
        user = await verify_jwt(token_original)
        assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
        assert route.call_count == 2

        # fetched_at must be untouched by the degenerate attempt.
        assert auth_mod._jwks_cache is not None  # noqa: SLF001
        assert auth_mod._jwks_cache[1] == original_fetched_at  # noqa: SLF001

        # The degenerate-fetch cooldown (issue #147 follow-up) was stamped
        # by the fetch #2 attempt above; capture it so the next clock
        # advance clears both that cooldown AND the (still-expired) TTL.
        assert auth_mod._last_degenerate_fetch is not None  # noqa: SLF001
        degenerate_stamp = auth_mod._last_degenerate_fetch  # noqa: SLF001

        # Advance the clock only slightly further than the cooldown window
        # (measured from the degenerate stamp) — NOT merely +2.0s from the
        # original TTL expiry, which would still be inside the cooldown and
        # would wrongly skip fetch #3. Since fetched_at was never refreshed,
        # the cache is STILL considered expired relative to the ORIGINAL
        # timestamp, so this call must retry the fetch a third time —
        # proving the degenerate attempt didn't reset the TTL.
        monkeypatch.setattr(
            auth_mod,
            "_now",
            lambda: degenerate_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0,
        )
        await verify_jwt(token_original)
        call_count = route.call_count

    assert call_count == 3, (
        f"expected a third fetch after the degenerate attempt (fetched_at "
        f"must not have been refreshed), got {call_count} calls"
    )


# ---------------------------------------------------------------------------
# Test 26 — degenerate-fetch cooldown bounds a sustained-outage storm
# (issue #147 follow-up, safety review)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_degenerate_fetch_cooldown_bounds_repeated_verifies(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sustained Supabase degenerate-200 incident hit by many requests
    with a cold cache must drive at most ONE routine fetch (plus at most
    one rate-limited kid-miss forced-refresh fetch) per cooldown window —
    NOT one fetch per request (issue #147 follow-up, safety review).

    Call-count accounting for the FIRST (failing) ``verify_jwt`` call:
      fetch #1 — routine ``_get_jwks`` path, cold cache, degenerate.
                 Stamps ``_last_degenerate_fetch``.
      fetch #2 — ``_find_signing_key`` misses on the empty result, which
                 (unavoidably, given the existing kid-miss mechanism)
                 triggers ``_refresh_jwks_on_kid_miss``; also degenerate.
                 Stamps ``_last_forced_refresh``.
    Both cooldowns are now active. Several MORE ``verify_jwt`` calls,
    still within the window, must add ZERO further fetches: the routine
    path's cooldown check short-circuits before fetching, and (since
    ``_jwks_cache`` is still ``None``) ``_find_signing_key`` misses again,
    landing on the also-rate-limited kid-miss fallback — no fetch there
    either. Total across all of these: still 2.

    Only once the clock advances past the window does the next verify
    drive a fresh fetch (fetch #3, a real JWKS this time).
    """
    token = _mint_token(private_key)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": []}),  # fetch #1: routine, cold cache
            httpx.Response(200, json={"keys": []}),  # fetch #2: kid-miss forced refresh
            httpx.Response(200, json=jwks_payload),  # fetch #3: after the window, succeeds
        ]
    )

    async with router:
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)
        assert exc_info.value.code == "invalid_token"
        assert route.call_count == 2

        # Several more verifies, still within the cooldown window: must
        # NOT add any further fetches (both cooldowns are active, and the
        # cache stays cold throughout — nothing to fall back to but the
        # degenerate result itself).
        for _ in range(4):
            with pytest.raises(AuthError) as exc_info:
                await verify_jwt(token)
            assert exc_info.value.code == "invalid_token"

        call_count_within_window = route.call_count
        assert call_count_within_window == 2, (
            f"expected the storm bounded at 2 total fetches (1 routine + 1 "
            f"kid-miss, both degenerate) across 5 verify_jwt calls, got "
            f"{call_count_within_window}"
        )

        # Advance the clock past both cooldown windows (they were stamped
        # within microseconds of each other, in the same failing call) so
        # the next verify fetches again.
        assert auth_mod._last_degenerate_fetch is not None  # noqa: SLF001
        stale_stamp = auth_mod._last_degenerate_fetch  # noqa: SLF001
        monkeypatch.setattr(
            auth_mod, "_now", lambda: stale_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0
        )

        user = await verify_jwt(token)  # fetch #3: cooldown expired, real keys
        call_count_after_window = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count_after_window == 3, (
        f"expected exactly one more fetch once the cooldown window passed, "
        f"got {call_count_after_window} total calls"
    )


# ---------------------------------------------------------------------------
# Test 27 — recovery clears the degenerate-fetch cooldown stamp, and a
# later incident starts a FRESH cooldown (issue #147 follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_degenerate_fetch_cooldown_stamp_cleared_on_recovery(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful (non-degenerate) fetch clears the degenerate-fetch
    cooldown stamp so recovery from an incident is instant, and so that a
    LATER, separate degenerate incident starts its own fresh cooldown
    rather than inheriting stale state (issue #147 follow-up).

    Proven behaviorally, not just via the module global: after recovery, a
    fresh degenerate incident's cooldown is shown to be ACTIVE (blocking a
    would-be fetch) shortly after IT starts.
    """
    token = _mint_token(private_key, kid=_KID)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": []}),  # fetch #1: routine, cold, degenerate
            httpx.Response(200, json={"keys": []}),  # fetch #2: kid-miss forced refresh, degenerate
            httpx.Response(200, json=jwks_payload),  # fetch #3: recovery, real keys
            httpx.Response(200, json={"keys": []}),  # fetch #4: a NEW, separate incident
        ]
    )

    async with router:
        # --- Incident 1: cold cache, degenerate, fails closed. ---
        with pytest.raises(AuthError):
            await verify_jwt(token)
        assert route.call_count == 2

        # --- Recovery: advance past the cooldown window, queue a good
        # response. The routine path fetches again and succeeds directly
        # (no kid-miss needed — the good JWKS contains the matching kid). ---
        assert auth_mod._last_degenerate_fetch is not None  # noqa: SLF001
        incident_1_stamp = auth_mod._last_degenerate_fetch  # noqa: SLF001
        monkeypatch.setattr(
            auth_mod,
            "_now",
            lambda: incident_1_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0,
        )
        user = await verify_jwt(token)
        assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
        assert route.call_count == 3
        assert auth_mod._last_degenerate_fetch is None  # noqa: SLF001

        # --- Force the routine path to refetch: jump past the FRESH 24h
        # TTL (relative to the recovery fetch) so a second, separate
        # degenerate incident can begin. ---
        assert auth_mod._jwks_cache is not None  # noqa: SLF001
        recovered_fetched_at = auth_mod._jwks_cache[1]  # noqa: SLF001
        incident_2_time = recovered_fetched_at + auth_mod._JWKS_TTL_SECONDS + 1.0
        monkeypatch.setattr(auth_mod, "_now", lambda: incident_2_time)

        # fetch #4 is degenerate; the stale-but-real cache from recovery is
        # kept, so the token still verifies (via the stale-keep fallback),
        # and a FRESH cooldown stamp is set at incident_2_time.
        user = await verify_jwt(token)
        assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
        assert route.call_count == 4
        assert auth_mod._last_degenerate_fetch == incident_2_time  # noqa: SLF001

        # --- Behavioral proof the new cooldown is genuinely fresh (not
        # leftover from incident 1, and not permanently disabled after
        # clearing): shortly after incident 2's stamp — well within a
        # fresh window — the next verify must be served from the
        # stale-but-real cache WITHOUT fetching again. Only 4 canned
        # responses are wired, so a 5th fetch attempt here would raise on
        # the exhausted side_effect iterator instead of this assertion. ---
        monkeypatch.setattr(auth_mod, "_now", lambda: incident_2_time + 1.0)
        await verify_jwt(token)
        call_count = route.call_count

    assert call_count == 4, (
        f"expected the post-recovery incident's cooldown to block a "
        f"further fetch shortly after it started, got {call_count} "
        "total calls"
    )


# ---------------------------------------------------------------------------
# Test 28 — the previously-"unreachable" rate-limited fallback in
# _refresh_jwks_on_kid_miss IS reachable (issue #147 follow-up,
# spec-guardian finding)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_second_verify_within_window_hits_kid_miss_rate_limited_fallback(
    private_key: EllipticCurvePrivateKey,
) -> None:
    """The specific path spec-guardian flagged in the #147 review: a cold
    cache whose routine fetch AND kid-miss forced-refresh are BOTH
    degenerate, followed by a second ``verify_jwt`` call still inside both
    rate-limit windows, must land on ``_refresh_jwks_on_kid_miss``'s
    rate-limited fallback with ``_jwks_cache`` still ``None`` — the branch
    whose comment previously (incorrectly) called it unreachable in
    production.

    Fails closed, and drives NO further fetches: only 2 canned responses
    are wired, so a third fetch attempt would raise on the exhausted
    side_effect iterator rather than this test's own assertion.
    """
    token = _mint_token(private_key)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": []}),  # fetch #1: routine, cold, degenerate
            httpx.Response(200, json={"keys": []}),  # fetch #2: kid-miss forced refresh, degenerate
        ]
    )

    async with router:
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)
        assert exc_info.value.code == "invalid_token"
        assert route.call_count == 2
        assert auth_mod._jwks_cache is None  # noqa: SLF001

        # Second verify, still within BOTH the degenerate-fetch cooldown
        # and the forced-refresh rate-limit window: the routine path skips
        # its fetch (cooldown active), _find_signing_key misses on the
        # resulting {"keys": []}, and _refresh_jwks_on_kid_miss's
        # rate-limited fallback returns {"keys": []} too (still nothing to
        # fall back to) — all without touching the network again.
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)
        assert exc_info.value.code == "invalid_token"
        call_count = route.call_count

    assert call_count == 2, (
        f"expected no further fetches on the second, still-rate-limited "
        f"verify, got {call_count} total calls"
    )


# ---------------------------------------------------------------------------
# Test 29 — a body of {} (no "keys" field at all) is treated like
# {"keys": []} on the routine _get_jwks path (issue #147 follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_keys_field_treated_as_degenerate_on_routine_path(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``{}`` (the ``"keys"`` field entirely absent, vs. present-but-empty)
    must be treated exactly like ``{"keys": []}`` by the routine
    ``_get_jwks`` guard — ``dict.get("keys")`` returns ``None`` either way,
    which is equally falsy. Not cached; the next call (after the cooldown
    window) retries the fetch.
    """
    token = _mint_token(private_key)

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={}),  # fetch #1: routine, cold cache, "keys" absent
            httpx.Response(200, json={}),  # fetch #2: kid-miss forced refresh, "keys" absent
            httpx.Response(200, json=jwks_payload),  # fetch #3: retried, succeeds
        ]
    )

    async with router:
        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(token)
        assert exc_info.value.code == "invalid_token"
        assert auth_mod._jwks_cache is None  # noqa: SLF001

        assert auth_mod._last_degenerate_fetch is not None  # noqa: SLF001
        stale_stamp = auth_mod._last_degenerate_fetch  # noqa: SLF001
        monkeypatch.setattr(
            auth_mod, "_now", lambda: stale_stamp + auth_mod._FORCED_REFRESH_WINDOW_SECONDS + 1.0
        )

        user = await verify_jwt(token)
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 3


# ---------------------------------------------------------------------------
# Test 30 — a body of {} (no "keys" field at all) does not clobber a
# known-good cache via _refresh_jwks_on_kid_miss (issue #147 follow-up)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_keys_field_treated_as_degenerate_on_kid_miss_path(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """``{}`` (the ``"keys"`` field entirely absent) reaching
    ``_refresh_jwks_on_kid_miss`` must be treated exactly like
    ``{"keys": []}``: it must NOT clobber a known-good cache. Mirrors test
    22, with ``{}`` instead of ``{"keys": []}`` as the degenerate body.
    """
    token_original = _mint_token(private_key, kid=_KID)
    unknown_token = _mint_token(private_key, kid="unknown-kid-missing-keys-field")

    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    route = router.get(_JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json=jwks_payload),  # fetch #1: prime (key A present)
            httpx.Response(200, json={}),  # fetch #2: forced refresh, "keys" field absent
        ]
    )

    async with router:
        await verify_jwt(token_original)  # fetch #1 — primes the cache with key A

        with pytest.raises(AuthError) as exc_info:
            await verify_jwt(unknown_token)  # fetch #2: forced refresh, {} body
        assert exc_info.value.code == "invalid_token"

        # The ORIGINAL kid must still verify: the {} response must not
        # have clobbered the cache, and no further fetch should have been
        # attempted (cache is intact).
        user = await verify_jwt(token_original)
        call_count = route.call_count

    assert user.user_id == UUID("11111111-1111-1111-1111-111111111111")
    assert call_count == 2, (
        f"expected exactly 2 fetches (prime + one {{}} attempt), got {call_count}"
    )
