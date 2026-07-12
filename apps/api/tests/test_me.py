"""Integration tests for GET /v1/me (issue #11).

Markers: ``integration`` — requires docker-compose Postgres + alembic upgrade head.

Harness
-------
- In-test ES256 keypair (same pattern as ``tests/test_auth.py``).
- respx mocks the JWKS endpoint so ``supabase_auth.verify_jwt`` works
  without a real Supabase project.
- ``httpx.AsyncClient`` with ``ASGITransport`` hits the live FastAPI app,
  which in turn uses the real Postgres from docker-compose.

Run:
    docker compose up -d
    DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
        uv run pytest tests/test_me.py -m integration -v

Each test:
- Generates a unique ``sub`` UUID to avoid cross-test row collisions.
- Deletes the created ``landlords`` row in teardown so the suite is
  re-runnable without wiping the DB.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
import respx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.db.session as db_mod
import app.integrations.supabase_auth as auth_mod
from app.main import app

# ---------------------------------------------------------------------------
# Constants — must match conftest.py placeholder env (or the live DB override)
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-001"
_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


# ---------------------------------------------------------------------------
# Key helpers (mirrored from test_auth.py to keep tests self-contained)
# ---------------------------------------------------------------------------

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
    private = ec.generate_private_key(ec.SECP256R1())
    return private, private.public_key()


def _public_key_to_jwks(public: EllipticCurvePublicKey, kid: str) -> dict[str, Any]:
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
    sub: str,
    email: str | int | None = "tenant@example.com",
    full_name: str | None = "Test Landlord",
    kid: str = _KID,
    exp_offset: int = 3600,
) -> str:
    """Mint a test JWT.

    ``email`` handling distinguishes three cases that must be normalized
    identically by ``supabase_auth.verify_jwt`` (issue #135 safety review):

    - ``email=None`` — omit the claim entirely (a token that never had one).
    - ``email=""`` / ``email="   "`` — encode the claim as present but empty
      or whitespace-only, matching real GoTrue behaviour: it serializes the
      JWT ``email`` claim from a Go string with no ``omitempty``, so a real
      phone-only signup emits ``"email": ""`` rather than an absent claim.
    - ``email=<non-string>`` (e.g. an int) — a malformed/junk claim value.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": _ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + exp_offset,
    }
    if email is not None:
        payload["email"] = email
    if full_name is not None:
        payload["user_metadata"] = {"full_name": full_name}
    return jwt.encode(payload, private, algorithm="ES256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Session-scoped migration — runs once per test session
# ---------------------------------------------------------------------------


def _alembic(*args: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "DATABASE_URL": _get_db_url()},
    )
    if result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"alembic {cmd!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    """Apply migrations once per session so the landlords table exists."""
    _alembic("upgrade", "head")
    yield


# ---------------------------------------------------------------------------
# Per-test engine and session for teardown cleanup
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Keypair + JWKS fixtures (function-scoped — new keypair each test)
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
def jwks_payload(
    keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey],
) -> dict[str, Any]:
    return _public_key_to_jwks(keypair[1], _KID)


@pytest.fixture(autouse=True)
def reset_jwks_cache() -> None:
    """Clear the module-level JWKS cache before each test."""
    auth_mod._jwks_state.cache = None  # noqa: SLF001


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """Dispose the module-level SQLAlchemy engine before and after each test.

    ``asyncio_default_fixture_loop_scope=function`` means each test runs in
    its own event loop.  If any prior test (e.g. ``test_readyz``) left
    asyncpg connections in the module-level pool, those connections are
    bound to that test's (now-closed) event loop.  When the current test
    creates a new event loop and tries to use the pool, asyncpg raises:

        RuntimeError: got Future ... attached to a different loop

    Disposing before the test ensures the pool is empty when we enter, so
    new connections are created in the current event loop.  Disposing after
    the test keeps the pool clean for the next test.
    """
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


# ---------------------------------------------------------------------------
# Helper: DELETE a landlord row by auth_user_id after a test
# ---------------------------------------------------------------------------


async def _cleanup(session: AsyncSession, auth_user_id: str) -> None:
    await session.execute(
        text("DELETE FROM landlords WHERE auth_user_id = :uid"),
        {"uid": auth_user_id},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_me_first_call_creates_row_and_returns_200(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """First call for a new auth_user_id → 200, landlords row created."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="first@example.com", full_name="Alice")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert response.status_code == 200
        body = response.json()

        # Shape checks
        assert "id" in body
        assert "email" in body
        assert "full_name" in body
        assert "timezone" in body
        assert "voice_profile" in body
        assert "price_cohort" in body
        assert "subscription_tier" in body
        assert "subscription_status" in body
        assert "created_at" in body

        # Value checks
        assert body["email"] == "first@example.com"
        assert body["full_name"] == "Alice"
        assert body["timezone"] == "America/Toronto"
        assert body["price_cohort"] == "early_access"
        assert body["subscription_tier"] == "free"
        assert body["subscription_status"] == "none"
        assert body["voice_profile"] is None

        # id must be a valid UUID string
        parsed_id = uuid.UUID(body["id"])
        assert isinstance(parsed_id, uuid.UUID)

        # DB must have exactly one row for this auth_user_id
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"),
            {"uid": sub},
        )
        count = result.scalar_one()
        assert count == 1
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_second_call_returns_same_id(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Two calls with the same token → same landlords.id, no duplicate row."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="idempotent@example.com")

    jwks_response = httpx.Response(200, json=jwks_payload)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=jwks_response)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            # Reset JWKS cache so the second call re-fetches (both succeed).
            auth_mod._jwks_state.cache = None  # noqa: SLF001
            r2 = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert r1.status_code == 200
        assert r2.status_code == 200

        id1 = r1.json()["id"]
        id2 = r2.json()["id"]
        assert id1 == id2, f"Two calls returned different landlord ids: {id1} vs {id2}"

        # Exactly one row in the DB
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"),
            {"uid": sub},
        )
        count = result.scalar_one()
        assert count == 1, f"Expected 1 landlords row, found {count}"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_email_sync_on_second_call(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Email changed in the token between calls → second call reflects new email.
    full_name omitted on second call → stored name is retained (COALESCE).
    """
    sub = str(uuid.uuid4())
    token1 = _mint_token(private_key, sub=sub, email="old@example.com", full_name="Bob Landlord")
    # Second token: new email, no full_name in user_metadata
    token2 = _mint_token(private_key, sub=sub, email="new@example.com", full_name=None)

    jwks_response = httpx.Response(200, json=jwks_payload)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=jwks_response)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/v1/me", headers={"Authorization": f"Bearer {token1}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001
            r2 = await client.get("/v1/me", headers={"Authorization": f"Bearer {token2}"})

    try:
        assert r1.status_code == 200
        assert r2.status_code == 200

        body1 = r1.json()
        body2 = r2.json()

        # Same id across both calls
        assert body1["id"] == body2["id"]

        # First call: original email and full_name
        assert body1["email"] == "old@example.com"
        assert body1["full_name"] == "Bob Landlord"

        # Second call: email synced from new token; full_name retained via COALESCE
        assert body2["email"] == "new@example.com"
        assert body2["full_name"] == "Bob Landlord", (
            "full_name should be retained when the new token omits user_metadata"
        )
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_no_auth_header_returns_401() -> None:
    """No Authorization header → 401 with the standard error envelope."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/me")

    assert response.status_code == 401
    body = response.json()
    assert "error" in body
    error = body["error"]
    assert "code" in error
    assert "message" in error
    assert "request_id" in error
    assert error["code"] == "missing_token"


@pytest.mark.integration
async def test_me_response_excludes_internal_fields(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Response must NOT expose auth_user_id, phone, stripe_customer_id,
    updated_at, or deleted_at."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub)
    forbidden_keys = {"auth_user_id", "phone", "stripe_customer_id", "updated_at", "deleted_at"}

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert response.status_code == 200
        body_keys = set(response.json().keys())
        leaked = body_keys & forbidden_keys
        assert not leaked, f"Response leaks internal fields: {leaked}"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_concurrent_first_calls_no_duplicate_key(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Two concurrent first-time requests for the same sub must not raise
    a duplicate-key error and must result in exactly one landlords row."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="concurrent@example.com")

    async def _call_me() -> httpx.Response:
        # Each coroutine shares the single respx context opened below (per
        # issue #145) — it must NOT open its own nested respx.mock context.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    # Reset cache just before firing so both coroutines start with a cold cache.
    auth_mod._jwks_state.cache = None  # noqa: SLF001

    # A single respx.MockRouter wraps BOTH coroutines and the gather call
    # itself (per issue #145): two coroutines each opening their own
    # respx.mock context against the same JWKS URL on a cold cache can
    # unmock/mis-route the second fetch depending on interleaving, causing a
    # spurious invalid_token 401. The property under test is the DB upsert
    # (concurrent first calls -> no duplicate-key error, one landlord row),
    # so we deliberately do NOT pre-warm the cache with a call before the
    # concurrent pair. Depending on how the two coroutines interleave around
    # ``_jwks_state.lock``, the JWKS route may be hit once or twice (both are
    # correct) — its call count is intentionally not asserted.
    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

    async with router:
        r1, r2 = await asyncio.gather(_call_me(), _call_me())

    try:
        assert r1.status_code == 200, f"First concurrent call failed: {r1.text}"
        assert r2.status_code == 200, f"Second concurrent call failed: {r2.text}"

        # Both must return the same landlord id.
        assert r1.json()["id"] == r2.json()["id"]

        # Exactly one row in the DB.
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"),
            {"uid": sub},
        )
        count = result.scalar_one()
        assert count == 1, f"Expected 1 landlords row after concurrent calls, found {count}"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_phone_only_token_returns_403_email_required(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """A verified token with no ``email`` claim (phone-only signup) must be
    rejected with 403 ``email_required`` via the standard error envelope,
    BEFORE any DB write — issue #135 part 2 (was a 500 NotNullViolation).
    """
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email=None)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert response.status_code == 403, response.text
        body = response.json()
        assert "error" in body
        error = body["error"]
        assert error["code"] == "email_required"
        assert "message" in error
        assert "request_id" in error

        # The message must be generic and never contain token material.
        assert token not in error["message"]
        assert sub not in error["message"]

        # Nothing written: no landlords row for this auth_user_id.
        result = await db_session.execute(
            text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"),
            {"uid": sub},
        )
        count = result.scalar_one()
        assert count == 0, f"Expected no landlords row to be written, found {count}"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_existing_landlord_token_loses_email_returns_403_row_untouched(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """An existing landlord row (created via a normal email token) must also
    be rejected with 403 ``email_required`` if a later token for the same
    ``sub`` has lost the email claim — never 200, never 500, and the row
    (including ``updated_at``) must be left completely untouched.
    """
    sub = str(uuid.uuid4())
    email_token = _mint_token(private_key, sub=sub, email="existing@example.com")
    phone_only_token = _mint_token(private_key, sub=sub, email=None)

    jwks_response = httpx.Response(200, json=jwks_payload)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=jwks_response)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed_response = await client.get(
                "/v1/me", headers={"Authorization": f"Bearer {email_token}"}
            )
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            row_before = (
                (
                    await db_session.execute(
                        text("SELECT email, updated_at FROM landlords WHERE auth_user_id = :uid"),
                        {"uid": sub},
                    )
                )
                .mappings()
                .one()
            )

            response = await client.get(
                "/v1/me", headers={"Authorization": f"Bearer {phone_only_token}"}
            )

    try:
        assert seed_response.status_code == 200, seed_response.text
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["error"]["code"] == "email_required"

        row_after = (
            (
                await db_session.execute(
                    text("SELECT email, updated_at FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )

        assert row_after["email"] == row_before["email"] == "existing@example.com"
        assert row_after["updated_at"] == row_before["updated_at"], (
            "Row must be untouched — updated_at changed after a rejected email_required call"
        )
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
@pytest.mark.parametrize(
    "junk_email",
    ["", "   ", 12345],
    ids=["empty_string", "whitespace_only", "non_string"],
)
async def test_me_junk_email_claim_returns_403_email_required(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    junk_email: str | int,
) -> None:
    """A token whose ``email`` claim is *present but unusable* — empty,
    whitespace-only, or non-string — must be normalized to "no email" by
    ``verify_jwt`` and rejected exactly like an entirely absent claim
    (issue #135 safety review).

    GoTrue serializes the JWT ``email`` claim from a Go string with no
    ``omitempty``, so a real phone-only signup emits ``"email": ""``
    (present, empty) rather than omitting the claim — an ``is None`` check
    alone would miss this and let it reach the upsert. Must be 403
    ``email_required``, never 200 and never a 500 NotNullViolation, and
    nothing written.
    """
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email=junk_email)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "email_required"

        result = await db_session.execute(
            text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"),
            {"uid": sub},
        )
        count = result.scalar_one()
        assert count == 0, f"Expected no landlords row to be written, found {count}"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_existing_landlord_empty_string_email_does_not_overwrite_stored_email(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Regression for the empty-string overwrite bug (issue #135 safety
    review): an existing landlord's stored email must NOT be silently
    clobbered to ``""`` via ``ON CONFLICT ... SET email = EXCLUDED.email``
    when a later token for the same ``sub`` carries a present-but-empty
    ``email`` claim. Must be 403 ``email_required``, and the stored email
    left byte-for-byte unchanged.
    """
    sub = str(uuid.uuid4())
    email_token = _mint_token(private_key, sub=sub, email="existing2@example.com")
    empty_email_token = _mint_token(private_key, sub=sub, email="")

    jwks_response = httpx.Response(200, json=jwks_payload)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=jwks_response)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed_response = await client.get(
                "/v1/me", headers={"Authorization": f"Bearer {email_token}"}
            )
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            response = await client.get(
                "/v1/me", headers={"Authorization": f"Bearer {empty_email_token}"}
            )

    try:
        assert seed_response.status_code == 200, seed_response.text
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "email_required"

        row = (
            (
                await db_session.execute(
                    text("SELECT email FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )
        assert row["email"] == "existing2@example.com", (
            f"Stored email must be unchanged, got {row['email']!r} — an empty-string "
            "email claim on a plain GET silently overwrote a real stored email"
        )
    finally:
        await _cleanup(db_session, sub)


# ---------------------------------------------------------------------------
# Soft-delete guard (issue #135 part 1) — resurrection is NOT allowed.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_me_soft_deleted_landlord_returns_403_account_deleted_row_untouched(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """A soft-deleted landlord (``deleted_at`` set) presenting an
    otherwise-valid, still-unexpired JWT must be rejected with 403
    ``account_deleted`` — resurrection is not allowed (issue #135 part 1,
    consistent with ``require_landlord``'s semantics for every other
    endpoint). ``email`` AND ``updated_at`` must be byte-identical before
    and after the rejected attempt: the upsert's ``ON CONFLICT ... WHERE
    deleted_at IS NULL`` guard must suppress the UPDATE atomically, not
    merely raise after already mutating the row.

    Seeds the soft-delete with a direct ``UPDATE landlords SET deleted_at``
    (the ``db_session`` fixture connects with no RLS scoping, i.e. as an
    admin/operator would) rather than driving it through the real
    ``auth.users`` trigger chain — that variant is covered separately by
    ``test_me_soft_deleted_landlord_via_auth_trigger_...`` below.
    """
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="soon-deleted@example.com")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed_response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            # Soft-delete directly, as an admin/operator (or migration
            # 0004's trigger) would — never a hard delete.
            await db_session.execute(
                text("UPDATE landlords SET deleted_at = now() WHERE auth_user_id = :uid"),
                {"uid": sub},
            )
            await db_session.commit()

            row_before = (
                (
                    await db_session.execute(
                        text("SELECT email, updated_at FROM landlords WHERE auth_user_id = :uid"),
                        {"uid": sub},
                    )
                )
                .mappings()
                .one()
            )

            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert seed_response.status_code == 200, seed_response.text
        assert response.status_code == 403, response.text
        body = response.json()
        assert "error" in body
        error = body["error"]
        assert error["code"] == "account_deleted"
        assert "message" in error
        assert "request_id" in error

        # Same stable code+message as require_landlord's account_deleted.
        assert error["message"] == "This account is no longer active."

        # Never leaks the token or the sub.
        assert token not in error["message"]
        assert sub not in error["message"]

        row_after = (
            (
                await db_session.execute(
                    text("SELECT email, updated_at FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )
        assert row_after["email"] == row_before["email"] == "soon-deleted@example.com"
        assert row_after["updated_at"] == row_before["updated_at"], (
            "Row must be untouched — updated_at changed after a rejected "
            "account_deleted attempt (resurrection must never mutate "
            "email/updated_at)"
        )
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_me_soft_deleted_landlord_via_auth_trigger_returns_403_row_untouched(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """Same guard, but seeded through the REAL migration-0004 ``auth.users``
    lifecycle trigger chain instead of a direct ``landlords`` mutation —
    exercises the actual path a live Supabase soft-delete would take:

    1. ``INSERT INTO auth.users`` (the local shim migration 0004 creates)
       fires ``on_auth_user_created`` -> provisions the ``landlords`` row.
    2. ``UPDATE auth.users SET deleted_at = now()`` fires
       ``on_auth_user_deleted_at_updated`` -> soft-deletes the matching
       ``landlords`` row.

    Only then is ``GET /v1/me`` called with a JWT for the same ``sub`` —
    must still be 403 ``account_deleted``, row untouched. Uses the direct
    path (``db_session`` against the shim ``auth.users`` table) rather than
    a real GoTrue call since none exists in this harness — cheap because
    the shim table already exists post-migration and ``db_session`` can
    reach it directly, same as ``tests/test_migrations_0004.py``.
    """
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="trigger-seeded@example.com")

    await db_session.execute(
        text("INSERT INTO auth.users (id, email) VALUES (:id, :email)"),
        {"id": sub, "email": "trigger-seeded@example.com"},
    )
    await db_session.execute(
        text("UPDATE auth.users SET deleted_at = now() WHERE id = :id"),
        {"id": sub},
    )
    await db_session.commit()

    row_before = (
        (
            await db_session.execute(
                text(
                    "SELECT email, updated_at, deleted_at FROM landlords WHERE auth_user_id = :uid"
                ),
                {"uid": sub},
            )
        )
        .mappings()
        .one()
    )
    assert row_before["email"] == "trigger-seeded@example.com"
    assert row_before["deleted_at"] is not None, (
        "precondition: the auth.users trigger chain must have already "
        "soft-deleted the landlords row before GET /v1/me is ever called"
    )

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    try:
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["error"]["code"] == "account_deleted"
        assert body["error"]["message"] == "This account is no longer active."
        assert "request_id" in body["error"]

        row_after = (
            (
                await db_session.execute(
                    text("SELECT email, updated_at FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )
        assert row_after["email"] == row_before["email"]
        assert row_after["updated_at"] == row_before["updated_at"]
    finally:
        await db_session.execute(text("DELETE FROM auth.users WHERE id = :id"), {"id": sub})
        await db_session.commit()
        await _cleanup(db_session, sub)


@pytest.mark.unit
def test_account_deleted_message_shared_with_require_landlord() -> None:
    """Regression pin: ``routers/me.py`` must import
    ``app.deps.ACCOUNT_DELETED_MESSAGE`` rather than re-declare its own
    literal copy, so the two ``account_deleted`` call sites (``GET /v1/me``
    and ``require_landlord``) can never silently drift apart.
    """
    import app.deps as deps_mod
    import app.routers.me as me_mod

    assert me_mod.ACCOUNT_DELETED_MESSAGE is deps_mod.ACCOUNT_DELETED_MESSAGE
    assert deps_mod.ACCOUNT_DELETED_MESSAGE == "This account is no longer active."


@pytest.mark.unit
def test_get_me_uses_admin_session_not_request_session() -> None:
    """Regression pin (#22 safety review, BLOCKING item 1): ``GET /v1/me``
    MUST depend on ``get_admin_session``, never ``get_session``/
    ``require_landlord``. Empirically reproduced: under an RLS-scoped
    session, both the brand-new-row INSERT and the existing-row
    ``ON CONFLICT DO UPDATE`` upsert are rejected by ``landlords``' own
    ``WITH CHECK`` policy (a freshly generated ``id`` can never equal a GUC
    set before that id exists) — provisioning is structurally
    incompatible with RLS scoping. See ``routers/me.py`` and
    ``app/db/session.py`` module docstrings for the full rationale.

    Inspects the live dependency wiring (not just the import statement) so
    a future edit that swaps the ``Depends(...)`` annotation without
    touching the import fails this test too. ``session`` is typed as
    ``Annotated[AsyncSession, Depends(...)]`` — the ``Depends`` marker
    lives in the annotation's metadata, not the parameter's plain
    ``.default`` (which is ``inspect.Parameter.empty`` for an
    ``Annotated``-only dependency). ``me.py`` has
    ``from __future__ import annotations`` (PEP 563), so annotations are
    strings at definition time — ``eval_str=True`` resolves them back to
    real objects (``Annotated[...]`` etc.) using the function's own module
    globals, otherwise ``.annotation`` here would just be a string with no
    ``__metadata__`` to inspect.
    """
    import inspect

    from fastapi.params import Depends

    from app.db.session import get_admin_session, get_session
    from app.routers.me import get_me

    session_param = inspect.signature(get_me, eval_str=True).parameters["session"]
    depends_markers = [
        meta
        for meta in getattr(session_param.annotation, "__metadata__", ())
        if isinstance(meta, Depends)
    ]
    assert len(depends_markers) == 1, (
        f"expected exactly one Depends(...) marker on `session`, found {depends_markers!r}"
    )
    dependency = depends_markers[0].dependency

    assert dependency is get_admin_session, (
        f"GET /v1/me's session dependency must be get_admin_session, got {dependency!r}"
    )
    assert dependency is not get_session


@pytest.mark.unit
def test_patch_me_uses_admin_session_not_request_session() -> None:
    """Regression pin mirroring ``test_get_me_uses_admin_session_not_
    request_session`` above: ``PATCH /v1/me`` MUST also depend on
    ``get_admin_session``, never ``get_session``/``require_landlord``. Same
    rationale as GET — this is still the ``/v1/me`` provisioning surface
    (module docstring's "Session" section), and a future drive-by "simplify
    this to require_landlord" edit must fail loudly HERE, at review/CI time,
    not silently at the production role-separation flip (the exact failure
    class the #54/#55/#57 spec review's CRITICAL finding on
    ``require_landlord`` itself was about).
    """
    import inspect

    from fastapi.params import Depends

    from app.db.session import get_admin_session, get_session
    from app.routers.me import update_me

    session_param = inspect.signature(update_me, eval_str=True).parameters["session"]
    depends_markers = [
        meta
        for meta in getattr(session_param.annotation, "__metadata__", ())
        if isinstance(meta, Depends)
    ]
    assert len(depends_markers) == 1, (
        f"expected exactly one Depends(...) marker on `session`, found {depends_markers!r}"
    )
    dependency = depends_markers[0].dependency

    assert dependency is get_admin_session, (
        f"PATCH /v1/me's session dependency must be get_admin_session, got {dependency!r}"
    )
    assert dependency is not get_session


# ---------------------------------------------------------------------------
# PATCH /v1/me (issue #57 — account settings / notification prefs)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_me_updates_full_name_and_timezone(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch1@example.com", full_name="Original")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            response = await client.patch(
                "/v1/me",
                json={"full_name": "Updated Name", "timezone": "America/Vancouver"},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["full_name"] == "Updated Name"
        assert body["timezone"] == "America/Vancouver"
        assert body["id"] == seed.json()["id"]

        row = (
            (
                await db_session.execute(
                    text("SELECT full_name, timezone FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )
        assert row["full_name"] == "Updated Name"
        assert row["timezone"] == "America/Vancouver"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_partial_update_preserves_other_fields(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch2@example.com", full_name="Keep Me")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            response = await client.patch(
                "/v1/me",
                json={"phone": "+14165551234"},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert response.status_code == 200, response.text
        body = response.json()
        # full_name untouched by a PATCH that never mentioned it.
        assert body["full_name"] == "Keep Me"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_no_op_body_returns_current_profile(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """An empty PATCH body (or one with no recognized keys) is a no-op read,
    never a write — and an unrecognized/extra key (e.g. a client mistakenly
    trying to flip emergency notifications off) is silently ignored rather
    than erroring, since ``MeUpdateRequest`` has no such field to set in the
    first place (api-contracts.md: "Emergency notifications are not a
    settable preference")."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch3@example.com", full_name="Untouched")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            response = await client.patch(
                "/v1/me",
                json={"emergency_notifications": False},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert response.status_code == 200, response.text
        assert response.json()["full_name"] == "Untouched"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_voice_profile_writes_audit_log_once(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch4@example.com")
    voice_profile = {"tone": "warm, direct", "samples": ["Hey! Thanks for flagging."]}

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            r1 = await client.patch(
                "/v1/me",
                json={"voice_profile": voice_profile},
                headers={"Authorization": f"Bearer {token}"},
            )
            auth_mod._jwks_state.cache = None  # noqa: SLF001
            # Re-patching with the SAME value must not write a second audit row.
            r2 = await client.patch(
                "/v1/me",
                json={"voice_profile": voice_profile},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert r1.json()["voice_profile"] == voice_profile

        landlord_id = r1.json()["id"]
        rows = (
            await db_session.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE landlord_id = :lid "
                    "AND action = 'settings_changed'"
                ),
                {"lid": landlord_id},
            )
        ).scalar_one()
        assert rows == 1

        await db_session.execute(
            text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
        )
        await db_session.commit()
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_timezone_null_rejected_422(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch5@example.com")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            response = await client.patch(
                "/v1/me",
                json={"timezone": None},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_field"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_never_provisioned_returns_403_account_deleted(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    """PATCH never lazily provisions — a brand-new auth user who PATCHes
    before ever GETting has no landlords row, and must get the same
    ``account_deleted`` 403 as a soft-deleted account (never a 500, never a
    silent row creation)."""
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch6@example.com")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                "/v1/me",
                json={"full_name": "Should not be written"},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "account_deleted"

        count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM landlords WHERE auth_user_id = :uid"), {"uid": sub}
            )
        ).scalar_one()
        assert count == 0, "PATCH /v1/me must never create a landlords row"
    finally:
        await _cleanup(db_session, sub)


@pytest.mark.integration
async def test_patch_me_soft_deleted_returns_403_account_deleted_row_untouched(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
) -> None:
    sub = str(uuid.uuid4())
    token = _mint_token(private_key, sub=sub, email="patch7@example.com")

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            seed = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
            auth_mod._jwks_state.cache = None  # noqa: SLF001

            await db_session.execute(
                text("UPDATE landlords SET deleted_at = now() WHERE auth_user_id = :uid"),
                {"uid": sub},
            )
            await db_session.commit()

            row_before = (
                (
                    await db_session.execute(
                        text(
                            "SELECT full_name, updated_at FROM landlords WHERE auth_user_id = :uid"
                        ),
                        {"uid": sub},
                    )
                )
                .mappings()
                .one()
            )

            response = await client.patch(
                "/v1/me",
                json={"full_name": "Should not apply"},
                headers={"Authorization": f"Bearer {token}"},
            )

    try:
        assert seed.status_code == 200, seed.text
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "account_deleted"

        row_after = (
            (
                await db_session.execute(
                    text("SELECT full_name, updated_at FROM landlords WHERE auth_user_id = :uid"),
                    {"uid": sub},
                )
            )
            .mappings()
            .one()
        )
        assert row_after["full_name"] == row_before["full_name"]
        assert row_after["updated_at"] == row_before["updated_at"]
    finally:
        await _cleanup(db_session, sub)
