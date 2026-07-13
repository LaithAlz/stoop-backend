"""Request-ID middleware.

Reads ``X-Request-ID`` from the incoming request (or generates a fresh
``req_<hex32>`` id when absent), binds it to structlog's contextvars so
that every log line emitted during the request carries ``request_id``,
and echoes it back in the ``X-Request-ID`` response header.

The ``req_`` prefix + 32-char hex body matches the example in
``docs/03-engineering/api-contracts.md``'s error envelope. A well-formed
client-supplied id is honored and echoed back **as-is** (no prefix added)
so it still works as the caller's own correlation id; the prefix only
applies to ids this service generates itself.

Also binds ``request_path`` and ``request_method`` so that log lines
contain enough context to grep/filter without touching request headers
(which must never be logged because they contain Authorization / JWTs).

Safety: we never bind or log ``request.headers``.

Ack-route path scrubbing (safety review, 2026-07-12, finding 6, MEDIUM)
--------------------------------------------------------------------------
``GET``/``POST /ack/{token}`` carry the ack token directly in the path.
Binding ``request.url.path`` verbatim (as every other route safely does —
none of them carry a capability token in the path) would put that token
into ``request_id``/every log line's bound context for the DURATION of
the request, including lines emitted deep inside the handler. This
middleware runs BEFORE routing resolves, so the matched route's PATTERN
(e.g. the literal string ``/ack/{token}``) isn't available yet — rather
than reconstructing Starlette's full route-matching logic here, a narrow,
targeted substitution (:data:`_ACK_PATH_RE`) replaces exactly an
``/ack/<anything>`` path with the literal pattern string before binding,
for this one route shape. Every other path is bound unchanged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

import structlog
import structlog.contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# A client-supplied X-Request-ID is untrusted: it is echoed into a response
# header and into every log line for the request. Restrict it to a safe
# charset and length so it can't be used for HTTP response-splitting (CRLF),
# log bloat, or control-character injection. Anything else is discarded and
# a fresh ``req_<hex32>`` id is generated instead. The bound (1, 128)
# comfortably accommodates our own generated format (``req_`` + 32 hex
# chars = 36) as well as reasonable client-supplied correlation ids.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Matches exactly `/ack/{anything}` (no further path segments) -- the ONE
# route shape in this app whose path parameter is a capability token, not
# an opaque/harmless id. See module docstring "Ack-route path scrubbing".
_ACK_PATH_RE = re.compile(r"^/ack/[^/]+$")
_ACK_PATH_PATTERN = "/ack/{token}"


def _generate_request_id() -> str:
    """A fresh server-generated id: ``req_`` + 32 lowercase hex chars."""
    return f"req_{uuid.uuid4().hex}"


def _safe_request_path(path: str) -> str:
    """Return *path* unchanged, EXCEPT an ``/ack/{token}`` path is replaced
    with the literal pattern string — see module docstring "Ack-route path
    scrubbing"."""
    if _ACK_PATH_RE.match(path):
        return _ACK_PATH_PATTERN
    return path


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Per-request correlation-ID middleware.

    Lifecycle per request:
      1. Clear any stale contextvars from a previous request on this worker.
      2. Read a well-formed ``X-Request-ID`` header, else generate a
         ``req_<hex32>`` id.
      3. Bind ``request_id``, ``request_path``, ``request_method`` to
         structlog contextvars.
      4. Call the next handler.
      5. Set ``X-Request-ID`` on the response.
      6. Clear contextvars (belt-and-suspenders cleanup).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Step 1 — clear stale context from a previous request.
        structlog.contextvars.clear_contextvars()

        # Step 2 — read the client-supplied request ID if it is well-formed,
        # otherwise generate a fresh req_<hex32> id. Untrusted values are
        # never echoed or logged verbatim. A well-formed client id is used
        # AS-IS (no req_ prefix added) — it is the caller's own correlation
        # id, not one we minted.
        supplied = request.headers.get("X-Request-ID")
        request_id = (
            supplied if supplied and _REQUEST_ID_RE.match(supplied) else _generate_request_id()
        )

        # Step 3 — bind to contextvars (NOT headers — headers contain JWTs).
        # request_path is scrubbed for the one route shape that carries a
        # capability token in its path -- see module docstring "Ack-route
        # path scrubbing".
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            request_path=_safe_request_path(request.url.path),
            request_method=request.method,
        )

        # Step 4 — hand off to the next handler.
        response: Response = await call_next(request)

        # Step 5 — echo the request ID back to the caller.
        response.headers["X-Request-ID"] = request_id

        # Step 6 — clean up so the next request starts fresh.
        structlog.contextvars.clear_contextvars()

        return response
