"""Tests for ``app/middleware/request_id.py``'s ack-route path scrubbing
(safety review, 2026-07-12, finding 6, MEDIUM) — the ack token must never
end up bound into ``request_path`` (and therefore into every log line for
that request).

Exercises ``RequestIDMiddleware.dispatch`` DIRECTLY (a fake ``call_next``
captures ``structlog.contextvars.get_contextvars()`` at the moment it
runs) rather than making a real HTTP call through an endpoint and
inspecting captured log output. This is deliberate, not a shortcut: a
structlog-log-based version of this test was tried first and is
GENUINELY FLAKY in the full suite — ``cache_logger_on_first_use=True``
(set once, process-wide, the first time ``app.main`` is imported for
real) means whichever module's logger proxy is used for the very first
time ANYWHERE in the whole session freezes itself to whatever
processors/logger were active at that instant; if some OTHER, earlier
test in the suite is the first to trigger
``app.routers.notifications``'s logger (with caching already on, outside
any ``capture_logs()`` scope), that proxy is permanently bound to a
plain ``PrintLogger`` from then on, and THIS test's own
``structlog.testing.capture_logs()`` — which only swaps the processor
list, never touches the already-frozen proxy — silently never sees the
event at all. Testing the middleware directly sidesteps the entire class
of hazard: no logger proxy, no structlog global config, no caching, just
a plain contextvars read.
"""

from __future__ import annotations

import pytest
import structlog.contextvars
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.request_id import RequestIDMiddleware, _safe_request_path


def _build_request(path: str, *, query_string: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("testclient", 12345),
        "http_version": "1.1",
    }
    return Request(scope)


async def _dispatch_and_capture(path: str) -> dict[str, object]:
    """Run the middleware's ``dispatch`` for a GET to *path*, returning
    whatever contextvars were bound at the moment ``call_next`` ran (i.e.
    exactly what any log line emitted from inside the "handler" would
    have seen)."""
    middleware = RequestIDMiddleware(app=None)  # type: ignore[arg-type]
    captured: dict[str, object] = {}

    async def _fake_call_next(_request: Request) -> Response:
        captured.update(structlog.contextvars.get_contextvars())
        return Response("ok")

    request = _build_request(path)
    await middleware.dispatch(request, _fake_call_next)
    return captured


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/ack/AbCdEf0123456789", "/ack/{token}"),
        ("/ack/tok-with-dashes_and_underscores", "/ack/{token}"),
        ("/healthz", "/healthz"),
        ("/webhooks/twilio/sms", "/webhooks/twilio/sms"),
        ("/v1/notifications/123/ack", "/v1/notifications/123/ack"),
        ("/ack/", "/ack/"),  # no token segment -- left unchanged
    ],
)
def test_safe_request_path(path: str, expected: str) -> None:
    assert _safe_request_path(path) == expected


@pytest.mark.unit
async def test_ack_route_binds_pattern_not_raw_token() -> None:
    """The middleware must bind ``request_path="/ack/{token}"`` — never
    the raw, secret-bearing path — for any ``/ack/<token>`` request."""
    secret_token = "supersecrettoken1234567890abcdef"  # noqa: S105 -- a fake ack token, not a password

    captured = await _dispatch_and_capture(f"/ack/{secret_token}")

    assert captured.get("request_path") == "/ack/{token}"
    assert secret_token not in repr(captured), "the raw ack token leaked into bound context"


@pytest.mark.unit
async def test_unrelated_route_keeps_its_real_path() -> None:
    """Sanity check: the scrubbing is narrowly scoped to /ack/* — every
    other route's real path is still bound unchanged."""
    captured = await _dispatch_and_capture("/webhooks/twilio/sms")
    assert captured.get("request_path") == "/webhooks/twilio/sms"


@pytest.mark.unit
async def test_dashboard_ack_route_path_is_left_unscrubbed() -> None:
    """Only the tokenized ``/ack/{token}`` link surface carries a
    capability token in its path — the authenticated dashboard endpoint's
    id is an opaque uuid, not a secret, and is left as-is."""
    path = "/v1/notifications/123e4567-e89b-12d3-a456-426614174000/ack"
    captured = await _dispatch_and_capture(path)
    assert captured.get("request_path") == path
