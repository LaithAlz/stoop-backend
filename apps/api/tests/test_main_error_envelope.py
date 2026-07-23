"""Tests for ``app/main.py``'s error-envelope exception handlers.

1. Unit test for the error-envelope merge order (#44/#45 safety review,
   LOW): ``exc.extra`` must be spread BEFORE the three reserved keys
   (``code``/``message``/``request_id``) in ``_app_error_handler``, so a
   caller can never override the statically-reviewed message via ``extra``.

2. Integration tests (marker ``integration``) for the global
   ``RequestValidationError`` handler (issue #219): a Pydantic validation
   failure on a request body must return the house error envelope
   (``{"error": {"code": "invalid_request", "message", "request_id"}}``),
   NOT FastAPI's own default ``{"detail": [...]}`` body — and must never
   echo a submitted (sentinel) value back into the response. Proven across
   TWO different validated routers (``devices.py``, ``drafts.py``) so the
   fix is demonstrably global, not router-specific.

Harness for the integration tests mirrors ``tests/test_me.py`` /
``tests/test_drafts_router.py`` exactly (in-test ES256 keypair, respx
-mocked JWKS, ``httpx.AsyncClient`` with ``ASGITransport`` against the
real FastAPI app) — kept self-contained per that module's own precedent.
"""

from __future__ import annotations

import base64
import json
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
from app.errors import AppError
from app.main import _app_error_handler, app

# ---------------------------------------------------------------------------
# Unit test — AppError extra/reserved-key merge order
# ---------------------------------------------------------------------------


def test_app_error_extra_can_never_override_reserved_keys() -> None:
    exc = AppError(
        status_code=409,
        code="draft_stale",
        message="Real, statically-reviewed message.",
        extra={
            "code": "hijacked",
            "message": "hijacked",
            "request_id": "hijacked",
            "fresh_draft_id": "abc-123",
        },
    )

    response = _app_error_handler(None, exc)  # type: ignore[arg-type]
    body = json.loads(response.body)

    assert body["error"]["code"] == "draft_stale"
    assert body["error"]["message"] == "Real, statically-reviewed message."
    # The endpoint-specific extra field IS present -- only the three
    # reserved keys are protected from an override.
    assert body["error"]["fresh_draft_id"] == "abc-123"


# ---------------------------------------------------------------------------
# Integration tests — global RequestValidationError -> house envelope (#219)
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-envelope-001"
_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"

# P-256 coordinates are always 32 bytes (RFC 7518 §6.2.1) -- same
# fixed-width encoding convention as every other test module's JWKS helper.
_P256_COORD_BYTES = 32


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _b64url(n: int, length: int) -> str:
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


def _mint_token(private: EllipticCurvePrivateKey, *, sub: str, kid: str = _KID) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": _ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        "email": "landlord@example.com",
    }
    return jwt.encode(payload, private, algorithm="ES256", headers={"kid": kid})


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
    _alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


@pytest.fixture()
def keypair() -> tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]:
    return _make_keypair()


@pytest.fixture()
def private_key(
    keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey],
) -> EllipticCurvePrivateKey:
    return keypair[0]


@pytest.fixture()
def jwks_payload(keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]) -> dict[str, Any]:
    return _public_key_to_jwks(keypair[1], _KID)


@pytest.fixture(autouse=True)
def reset_jwks_cache() -> None:
    """Reset the module-level JWKS cache before each test."""
    auth_mod._jwks_state.cache = None  # noqa: SLF001


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """Dispose the module-level SQLAlchemy engine before/after each test --
    see ``tests/test_me.py``'s identical fixture for the cross-event-loop
    rationale (``asyncio_default_fixture_loop_scope=function``)."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


async def _seed_landlord_with_auth(session: AsyncSession, *, auth_user_id: str) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": auth_user_id, "email": f"{landlord_id}@example.com"},
    )
    await session.commit()
    return landlord_id


async def _cleanup_landlord(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


def _make_jwks_router(jwks: dict[str, Any]) -> respx.MockRouter:
    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks))
    return router


# A distinctive marker that would only appear in the response if a raw
# submitted value were echoed back -- looks phone-number-ish so it also
# doubles as a stand-in for the kind of tenant PII a validated field could
# carry (never-break rule #5).
_SENTINEL = "SENTINEL-4165551234-DO-NOT-ECHO"


def _assert_house_envelope_422(body: dict[str, Any], response_text: str) -> None:
    assert "detail" not in body, "FastAPI's default 422 shape leaked ('detail' key present)"
    assert "error" in body
    error = body["error"]
    assert error["code"] == "invalid_request"
    assert isinstance(error["message"], str) and error["message"]
    assert "request_id" in error
    assert error["request_id"] is not None
    assert isinstance(error["request_id"], str)
    assert _SENTINEL not in response_text, "submitted sentinel value leaked into the 422 response"


# ---------------------------------------------------------------------------
# Router 1 — POST /v1/devices (app/routers/devices.py)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_devices_malformed_body_returns_house_envelope(
    db_session: AsyncSession,
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _make_jwks_router(jwks_payload):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/devices",
                    json={"token": "ExponentPushToken[abc]", "platform": _SENTINEL},
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        _assert_house_envelope_422(response.json(), response.text)
    finally:
        await _cleanup_landlord(db_session, landlord_id)


@pytest.mark.integration
async def test_devices_empty_token_returns_house_envelope(
    db_session: AsyncSession,
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """A second malformed-body shape on the SAME router (empty ``token``,
    rejected by the field validator, not just the ``Literal`` platform
    check) -- confirms the handler covers both a type/enum failure and a
    custom ``field_validator`` failure identically."""
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _make_jwks_router(jwks_payload):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/devices",
                    json={"token": "", "platform": "ios"},
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        body = response.json()
        assert "detail" not in body
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["request_id"] is not None
    finally:
        await _cleanup_landlord(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Router 2 — POST /v1/drafts/{id}/edit-and-send (app/routers/drafts.py)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_drafts_edit_and_send_malformed_body_returns_house_envelope(
    db_session: AsyncSession,
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """``EditAndSendRequest.body`` given a non-string (int) value -- a type
    -validation failure whose FastAPI-default ``"input"`` would otherwise
    echo the raw submitted value (here, a sentinel that stands in for e.g.
    a tenant phone number) straight into the response body.

    The referenced ``draft_id`` doesn't need to exist: FastAPI raises
    ``RequestValidationError`` (and this handler short-circuits to 422)
    before the endpoint function -- and therefore any DB lookup -- ever
    runs, proving the handler fires globally regardless of what the
    underlying resource lookup would have done.
    """
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)
    draft_id = uuid.uuid4()

    try:
        async with _make_jwks_router(jwks_payload):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/edit-and-send",
                    json={"body": 4165551234},  # sentinel-shaped int, not a string
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        body = response.json()
        _assert_house_envelope_422(body, response.text)
        assert "4165551234" not in response.text
    finally:
        await _cleanup_landlord(db_session, landlord_id)


@pytest.mark.integration
async def test_drafts_edit_and_send_missing_body_field_returns_house_envelope(
    db_session: AsyncSession,
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
) -> None:
    """A second malformed-body shape on the SAME router (the required
    ``body`` field omitted entirely) -- confirms the handler covers a
    "missing field" failure identically to a "wrong type" failure."""
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)
    draft_id = uuid.uuid4()

    try:
        async with _make_jwks_router(jwks_payload):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/edit-and-send",
                    json={},
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 422
        body = response.json()
        assert "detail" not in body
        assert body["error"]["code"] == "invalid_request"
        assert body["error"]["request_id"] is not None
    finally:
        await _cleanup_landlord(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Regression — AuthError (401) and AppError-style failures still work
# (this handler must ONLY reshape RequestValidationError, never any other
# exception type).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_auth_still_returns_401_not_422() -> None:
    """A request with no ``Authorization`` header must still 401 via
    ``AuthError`` -- proving the new ``RequestValidationError`` handler did
    not somehow intercept or shadow the existing auth error path."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/devices", json={"token": "x", "platform": "ios"})

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "missing_token"
