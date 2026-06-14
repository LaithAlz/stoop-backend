"""Tests for observability: structured logging, Sentry init, and request_id middleware.

Network-free design:
- Sentry init is tested by monkeypatching ``sentry_sdk.init`` — verify it
  is called (or not called) without touching the network.
- Sentry capture is tested via an in-memory transport that records events
  without sending them over the network.
- Structlog stdout output is captured via ``capsys`` after reconfiguring
  structlog to write to the real ``sys.stdout`` (which pytest intercepts).
- The error endpoint uses ``raise_app_exceptions=False`` on ASGITransport
  so exceptions are converted to 500 responses rather than re-raised.
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import structlog
import structlog.contextvars
from httpx import ASGITransport

from app.config import Settings
from app.observability import init_sentry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fresh_settings(**overrides: Any) -> Settings:
    """Construct a Settings instance without loading .env or the module singleton."""
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://t:t@localhost:5432/t",
        "supabase_url": "https://test.supabase.co",
        "supabase_jwks_url": "https://test.supabase.co/auth/v1/.well-known/jwks.json",
        "supabase_jwt_issuer": "https://test.supabase.co/auth/v1",
        "supabase_service_role_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[call-arg]


def _reconfigure_structlog_to_stdout() -> None:
    """Point structlog at the real sys.stdout (capsys can then intercept it)."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(10),  # DEBUG
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


# ---------------------------------------------------------------------------
# configure_logging — JSON output with request_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_configure_logging_produces_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """/_debug/log produces a JSON log line on stdout containing ``request_id``.

    Strategy: reconfigure structlog to write to the real sys.stdout (which
    pytest's capsys intercepts even in async tests), make one HTTP call,
    parse the captured output, and assert required fields are present.
    """
    _reconfigure_structlog_to_stdout()

    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/_debug/log")

    assert resp.status_code == 200

    captured = capsys.readouterr().out
    json_lines = [ln for ln in captured.splitlines() if ln.strip().startswith("{")]
    assert json_lines, "No JSON log lines found on stdout"

    events = [json.loads(ln) for ln in json_lines]
    debug_events = [e for e in events if e.get("event") == "debug_log_endpoint_called"]
    found = [e.get("event") for e in events]
    assert debug_events, f"debug_log_endpoint_called not found; got: {found}"

    evt = debug_events[0]
    assert "timestamp" in evt, "Missing 'timestamp'"
    assert "level" in evt, "Missing 'level'"
    assert "event" in evt, "Missing 'event'"
    assert "request_id" in evt, "Missing 'request_id' — context propagation broken"


# ---------------------------------------------------------------------------
# RequestIDMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_request_id_echoed_in_response() -> None:
    """X-Request-ID from client is round-tripped back in the response."""
    from app.main import app

    supplied_id = str(uuid.uuid4())
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": supplied_id})

    assert response.headers.get("x-request-id") == supplied_id


@pytest.mark.unit
async def test_missing_request_id_is_generated() -> None:
    """When X-Request-ID is absent, the middleware generates a valid uuid4."""
    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    generated = response.headers.get("x-request-id")
    assert generated is not None, "X-Request-ID not present in response"
    parsed = uuid.UUID(generated)
    assert parsed.version == 4


@pytest.mark.unit
async def test_supplied_request_id_preserved() -> None:
    """A client-supplied X-Request-ID is preserved unchanged (round-trips)."""
    from app.main import app

    my_id = "my-custom-request-id-abc123"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": my_id})

    assert response.headers.get("x-request-id") == my_id


# ---------------------------------------------------------------------------
# Sentry init gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_init_sentry_no_op_without_dsn() -> None:
    """init_sentry() must NOT call sentry_sdk.init when sentry_dsn is None."""
    with (
        patch("app.observability.settings") as mock_settings,
        patch("sentry_sdk.init") as mock_sdk_init,
    ):
        mock_settings.sentry_dsn = None
        mock_settings.is_production = False
        init_sentry()

    mock_sdk_init.assert_not_called()


@pytest.mark.unit
def test_init_sentry_calls_sdk_init_when_dsn_set() -> None:
    """init_sentry() calls sentry_sdk.init exactly once when sentry_dsn is set."""
    fake_dsn = "https://fake_key@o123.ingest.sentry.io/456"

    with (
        patch("app.observability.settings") as mock_settings,
        patch("sentry_sdk.init") as mock_sdk_init,
    ):
        mock_settings.sentry_dsn = fake_dsn
        mock_settings.is_production = False
        init_sentry()

    mock_sdk_init.assert_called_once()
    call_kwargs = mock_sdk_init.call_args.kwargs
    assert call_kwargs.get("dsn") == fake_dsn
    assert call_kwargs.get("send_default_pii") is False, (
        "send_default_pii must be False — prevents JWT/body leakage"
    )


@pytest.mark.unit
def test_init_sentry_send_default_pii_false_in_production() -> None:
    """send_default_pii must be False even in production — JWT/body leakage prevention."""
    fake_dsn = "https://fake_key@o123.ingest.sentry.io/456"

    with (
        patch("app.observability.settings") as mock_settings,
        patch("sentry_sdk.init") as mock_sdk_init,
    ):
        mock_settings.sentry_dsn = fake_dsn
        mock_settings.is_production = True
        init_sentry()

    call_kwargs = mock_sdk_init.call_args.kwargs
    assert call_kwargs.get("send_default_pii") is False


@pytest.mark.unit
def test_init_sentry_traces_sample_rate_production() -> None:
    """traces_sample_rate is 0.1 in production."""
    fake_dsn = "https://fake_key@o123.ingest.sentry.io/456"

    with (
        patch("app.observability.settings") as mock_settings,
        patch("sentry_sdk.init") as mock_sdk_init,
    ):
        mock_settings.sentry_dsn = fake_dsn
        mock_settings.is_production = True
        init_sentry()

    call_kwargs = mock_sdk_init.call_args.kwargs
    assert call_kwargs.get("traces_sample_rate") == 0.1


@pytest.mark.unit
def test_init_sentry_traces_sample_rate_dev() -> None:
    """traces_sample_rate is 1.0 in non-production."""
    fake_dsn = "https://fake_key@o123.ingest.sentry.io/456"

    with (
        patch("app.observability.settings") as mock_settings,
        patch("sentry_sdk.init") as mock_sdk_init,
    ):
        mock_settings.sentry_dsn = fake_dsn
        mock_settings.is_production = False
        init_sentry()

    call_kwargs = mock_sdk_init.call_args.kwargs
    assert call_kwargs.get("traces_sample_rate") == 1.0


# ---------------------------------------------------------------------------
# Error endpoint + Sentry capture (offline)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_debug_error_endpoint_returns_500() -> None:
    """/_debug/error raises an unhandled exception → ASGITransport returns 500.

    We set ``raise_app_exceptions=False`` so the exception is converted to a
    500 response instead of being re-raised into the test.
    """
    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/_debug/error")

    assert response.status_code == 500


@pytest.mark.unit
async def test_debug_error_sentry_capture_offline() -> None:
    """When a DSN is set, Sentry's FastAPI integration captures the error event.

    We provide a custom in-memory transport that records envelopes without
    hitting the network.  After the request, we flush and assert at least one
    event was captured.

    ``raise_app_exceptions=False`` on ASGITransport converts the propagated
    RuntimeError into a 500 response instead of crashing the test.
    """
    import sentry_sdk
    from sentry_sdk.transport import Transport

    captured_events: list[dict[str, Any]] = []

    class _MemoryTransport(Transport):
        def capture_envelope(self, envelope: Any) -> None:  # type: ignore[override]
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    captured_events.append(item.payload.json or {})

    # Exercise the REAL production init_sentry() — with its protective flags
    # (include_local_variables=False + before_send scrubber) — rather than a
    # hand-rolled init, so this test actually guards never-break rule #5.
    # We point settings at a fake DSN and inject the in-memory transport.
    fresh_settings = _make_fresh_settings(
        sentry_dsn="https://fake@o0.ingest.sentry.io/0",
    )

    jwt_sentinel = "SENTINEL.JWT.SHOULD-NOT-LEAK"
    cookie_sentinel = "session=SENTINEL-COOKIE-SHOULD-NOT-LEAK"

    try:
        with patch("app.observability.settings", fresh_settings):
            init_sentry(transport=_MemoryTransport())

        from app.main import app

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/_debug/error",
                headers={
                    "Authorization": f"Bearer {jwt_sentinel}",
                    "Cookie": cookie_sentinel,
                },
            )

        assert response.status_code == 500
        sentry_sdk.flush(timeout=2)
        assert captured_events, (
            "Expected Sentry to capture an error event; transport received nothing. "
            "Verify FastApiIntegration is wired correctly."
        )

        # never-break rule #5: the JWT / cookie must NOT appear anywhere in the
        # serialised event — not in request headers, not in stack-frame locals.
        serialised = json.dumps(captured_events)
        assert jwt_sentinel not in serialised, "JWT leaked into Sentry event"
        assert cookie_sentinel not in serialised, "Cookie leaked into Sentry event"
    finally:
        # Reset Sentry so it does not bleed into other tests.
        sentry_sdk.init(dsn=None)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_malformed_request_id_is_rejected() -> None:
    """A client X-Request-ID with illegal chars/length is replaced by a uuid4.

    Guards against HTTP response-splitting (CRLF), log forging, and log bloat
    from an unbounded attacker-controlled value.
    """
    from app.main import app

    for bad in (
        "abc\r\nX-Injected: evil",  # CRLF / response splitting
        "x" * 500,  # unbounded length
        "has spaces and !@#$",  # illegal charset
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/healthz", headers={"X-Request-ID": bad})

        echoed = response.headers.get("x-request-id")
        assert echoed is not None
        assert echoed != bad, "malformed X-Request-ID must not be echoed verbatim"
        # the replacement is a valid uuid4
        assert uuid.UUID(echoed).version == 4


# ---------------------------------------------------------------------------
# Debug endpoints gated in production
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_debug_endpoints_not_registered_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/_debug/* routes must return 404 when environment=production.

    We build a fresh app instance by patching ``settings`` in ``app.main``
    so that ``create_app()`` sees ``is_production=True``.  This avoids
    module-reload side-effects while still exercising the gating logic.
    """
    prod_settings = _make_fresh_settings(environment="production")

    from app.main import create_app

    with patch("app.main.settings", prod_settings):
        prod_app = create_app()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=prod_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        log_resp = await client.get("/_debug/log")
        err_resp = await client.get("/_debug/error")

    assert log_resp.status_code == 404, (
        f"/_debug/log should be 404 in production, got {log_resp.status_code}"
    )
    assert err_resp.status_code == 404, (
        f"/_debug/error should be 404 in production, got {err_resp.status_code}"
    )


# ---------------------------------------------------------------------------
# Existing health endpoints still pass with middleware in place
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_healthz_still_ok_with_middleware() -> None:
    """Health endpoints continue to work after RequestIDMiddleware is added."""
    from app.main import app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-request-id" in response.headers
