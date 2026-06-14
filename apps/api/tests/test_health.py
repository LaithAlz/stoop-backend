"""Smoke tests for /healthz and /readyz endpoints."""

import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.mark.unit
async def test_healthz_returns_200() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
async def test_readyz_returns_200() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
