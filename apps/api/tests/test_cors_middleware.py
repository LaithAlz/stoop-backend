"""Tests for ``app/main.py``'s CORS middleware (issue #251).

The web dashboard calls ``/v1/*`` cross-origin with an ``Authorization``
bearer header — without CORS middleware, every browser call fails
preflight (silence on the emergency surface, since the dashboard can't
load its queue). Separately, the undo-countdown UX anchors to the
response ``Date`` header, which browsers strip cross-origin unless it is
explicitly exposed via ``Access-Control-Expose-Headers``.

Exercised against ``/healthz`` (needs no DB/auth, mirrors
``tests/test_health.py``'s own harness) since these tests are about the
CORS middleware layer itself, not any particular router.

The allowlist under test is the DEV DEFAULT (``settings.dashboard_origins``
unset in the test environment — ``tests/conftest.py`` sets required env
vars only, never ``DASHBOARD_ORIGINS``) — ``http://localhost:5173`` and
``http://localhost:3000`` (``app/config.py``'s ``dashboard_origins``
field default).
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.main import app

_ALLOWED_ORIGIN = "http://localhost:5173"
_DISALLOWED_ORIGIN = "http://evil.example"


@pytest.mark.unit
def test_dev_default_allowlist_is_the_expected_two_origins() -> None:
    """Sanity check the fixture assumption every test below relies on --
    if this drifts, every other assertion in this file needs re-reading."""
    assert settings.dashboard_origins_list == [
        "http://localhost:5173",
        "http://localhost:3000",
    ]


# ---------------------------------------------------------------------------
# (a) Preflight OPTIONS from an allowed origin
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_preflight_from_allowed_origin_gets_acao_and_allow_headers() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.options(
            "/healthz",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN

    allow_methods = response.headers.get("access-control-allow-methods", "")
    for method in ("GET", "POST", "PATCH", "DELETE", "OPTIONS"):
        assert method in allow_methods, f"{method} missing from Access-Control-Allow-Methods"

    allow_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers


@pytest.mark.unit
async def test_preflight_expose_headers_contains_date_on_actual_responses() -> None:
    """``Access-Control-Expose-Headers`` must contain ``Date`` on a real
    (non-preflight) response from an allowed origin — the web
    undo-countdown anchors to the response ``Date`` header, which browsers
    otherwise strip cross-origin.

    Note: the actual ``Date`` header itself is added by the real HTTP
    server (uvicorn) at the wire level per HTTP/1.1 semantics — it is not
    present here because ``httpx.ASGITransport`` talks to the ASGI app
    in-process, with no real server constructing a status line/headers.
    This test only proves the CORS declaration this codebase controls
    (``Access-Control-Expose-Headers``); the header's real presence in
    production is standard HTTP server behavior, not something this app
    adds or could omit.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"Origin": _ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers.get("access-control-expose-headers") == "Date"


# ---------------------------------------------------------------------------
# (b) Disallowed origin -> no Access-Control-Allow-Origin
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_disallowed_origin_actual_request_gets_no_acao() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"Origin": _DISALLOWED_ORIGIN})

    # The request still succeeds at the HTTP layer (CORS is a browser-side
    # enforcement mechanism, not a server-side access-control mechanism) --
    # what matters is the ABSENCE of the header that would let a browser
    # hand the response body to that origin's JavaScript.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.unit
async def test_disallowed_origin_preflight_does_not_allow_origin() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.options(
            "/healthz",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers.get("access-control-allow-origin") != _DISALLOWED_ORIGIN
    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# (c) Plain GET with an allowed Origin
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_plain_get_with_allowed_origin_echoes_acao_and_exposes_date() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"Origin": _ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN
    assert response.headers.get("access-control-expose-headers") == "Date"
    # allow_credentials=False (bearer tokens, not cookies) -- this header
    # must never be sent.
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.unit
async def test_plain_get_with_second_allowed_origin_also_echoes_acao() -> None:
    """Both dev-default origins are independently allowed, not just the
    first one in the list."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"Origin": "http://localhost:3000"})

    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# (d) No Origin header -> no Access-Control-* headers at all (webhooks /
# server-to-server calls must be completely unaffected).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_no_origin_header_gains_no_cors_headers() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    cors_headers = [h for h in response.headers if h.lower().startswith("access-control-")]
    assert cors_headers == [], f"unexpected CORS headers on an Origin-less request: {cors_headers}"


@pytest.mark.unit
async def test_no_origin_header_on_webhook_route_gains_no_cors_headers() -> None:
    """Same guarantee on a real webhook-shaped path -- Twilio never sends
    an Origin header, and this must never grow CORS headers that could
    interfere with (or merely clutter) its signature-verified POSTs.
    A malformed/unsigned POST here still 400s/401s well before any
    Twilio-specific logic runs; only the header set matters for this test.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/webhooks/twilio/sms", data={})

    cors_headers = [h for h in response.headers if h.lower().startswith("access-control-")]
    assert cors_headers == [], (
        f"unexpected CORS headers on an Origin-less webhook POST: {cors_headers}"
    )


# ---------------------------------------------------------------------------
# F6 (#251 safety review) -- a HOSTILE Origin on the real webhook route.
# Twilio itself never sends an Origin header (covered above); this proves
# that even if some OTHER caller sent one, signature verification still
# 403s and no Access-Control-Allow-Origin is ever added for a disallowed
# origin -- an inert Access-Control-Expose-Headers is expected (Starlette's
# CORSMiddleware sets it unconditionally whenever Origin is present, see
# its own `simple_headers`/`send()`), so this asserts specifically on ACAO
# absence, not "no CORS headers at all".
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_webhook_with_hostile_origin_still_403s_and_gets_no_acao() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/twilio/sms", data={}, headers={"Origin": _DISALLOWED_ORIGIN}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_signature"
    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# F1 (#251 safety review, HIGH) -- an unhandled 500 is sent by Starlette's
# ServerErrorMiddleware, which sits OUTSIDE CORSMiddleware (installed
# Starlette version: 1.3.1) -- app/main.py's _unhandled_exception_handler
# sets Access-Control-Allow-Origin/Vary/X-Request-ID on that response
# itself, Origin-gated the same way CORSMiddleware gates every other
# response. Exercised via GET /_debug/error (non-production only router,
# same endpoint tests/test_main_error_envelope.py's own 500 tests use).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_unhandled_500_with_allowed_origin_gets_acao_and_request_id() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/_debug/error", headers={"Origin": _ALLOWED_ORIGIN})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == _ALLOWED_ORIGIN
    assert response.headers.get("vary") == "Origin"
    assert response.headers.get("x-request-id"), "X-Request-ID missing from a 500 response"


@pytest.mark.unit
async def test_unhandled_500_with_no_origin_gains_no_cors_headers() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/_debug/error")

    assert response.status_code == 500
    cors_headers = [h for h in response.headers if h.lower().startswith("access-control-")]
    assert cors_headers == [], f"unexpected CORS headers on an Origin-less 500: {cors_headers}"


@pytest.mark.unit
async def test_unhandled_500_with_disallowed_origin_gains_no_acao() -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/_debug/error", headers={"Origin": _DISALLOWED_ORIGIN})

    assert response.status_code == 500
    assert "access-control-allow-origin" not in response.headers
