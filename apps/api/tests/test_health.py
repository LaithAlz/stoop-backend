"""Tests for /healthz and /readyz endpoints.

Unit tests:
- /healthz always returns 200 (liveness, no DB).
- /readyz returns 503 with a generic, credential-free body when the DB is
  unreachable (engine.connect monkeypatched to raise OperationalError).

Integration tests (marker ``integration``, require docker-compose Postgres):
- /readyz returns 200 when the DB is reachable.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.exc import OperationalError

from app.main import app

# ---------------------------------------------------------------------------
# Liveness — always-up, no DB
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_healthz_returns_200() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Readiness — 503 path (unit, no real DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_readyz_returns_503_when_db_unreachable() -> None:
    """When engine.connect() raises OperationalError, /readyz must return 503.

    Strategy: monkeypatch ``app.routers.health.engine.connect`` to raise an
    OperationalError so the test is deterministic and requires no real DB.
    The context-manager protocol used in the handler (``async with engine.connect()``)
    means we patch ``connect`` with an async context manager that raises on enter.
    """

    class _FailingConn:
        async def __aenter__(self) -> _FailingConn:
            raise OperationalError("could not connect", {}, None)

        async def __aexit__(self, *args: object) -> None:
            pass

    with patch("app.routers.health.engine") as mock_engine:
        mock_engine.connect.return_value = _FailingConn()

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert "detail" in body

    # Never-break rule #5: the body must not contain any credentials or URL
    # (the monkeypatched error message above is also inert, but belt-and-braces)
    body_str = response.text
    assert "postgresql" not in body_str, "DB URL scheme leaked into 503 body"
    assert "stoop" not in body_str, "DB credential leaked into 503 body"
    assert "password" not in body_str.lower(), "password leaked into 503 body"


@pytest.mark.unit
async def test_readyz_503_body_has_no_dsn() -> None:
    """The 503 diagnostic body must NEVER contain DATABASE_URL or credentials.

    This test drives against the real settings.database_url value (which in
    the test environment is ``postgresql+asyncpg://test:test@localhost:5432/test``
    as set by conftest.py) to confirm nothing from it leaks into the response.
    """
    from app.config import settings

    dsn = settings.database_url
    # Extract individual tokens from the DSN that would be damaging if leaked.
    # Split out scheme, credentials, host, dbname.
    import re

    tokens = re.split(r"[/:@]", dsn)
    meaningful_tokens = [t for t in tokens if len(t) > 3]  # skip short ones

    class _FailingConn:
        async def __aenter__(self) -> _FailingConn:
            raise OperationalError(dsn, {}, None)  # worst case: DSN in the message

        async def __aexit__(self, *args: object) -> None:
            pass

    with patch("app.routers.health.engine") as mock_engine:
        mock_engine.connect.return_value = _FailingConn()

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/readyz")

    assert response.status_code == 503
    body_str = response.text

    for token in meaningful_tokens:
        assert token not in body_str, f"DSN token {token!r} leaked into the 503 response body"


# ---------------------------------------------------------------------------
# Readiness — 200 path (integration, requires docker-compose Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_readyz_returns_200_with_real_db() -> None:
    """With a live Postgres (docker-compose), /readyz must return 200.

    Run with:
        DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop \\
            uv run pytest tests/test_health.py -m integration -v
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
