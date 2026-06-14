"""Request-ID middleware.

Reads ``X-Request-ID`` from the incoming request (or generates a fresh
``uuid4`` when absent), binds it to structlog's contextvars so that every
log line emitted during the request carries ``request_id``, and echoes it
back in the ``X-Request-ID`` response header.

Also binds ``request_path`` and ``request_method`` so that log lines
contain enough context to grep/filter without touching request headers
(which must never be logged because they contain Authorization / JWTs).

Safety: we never bind or log ``request.headers``.
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
# a fresh uuid4 is generated instead.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Per-request correlation-ID middleware.

    Lifecycle per request:
      1. Clear any stale contextvars from a previous request on this worker.
      2. Read a well-formed ``X-Request-ID`` header, else generate a ``uuid4``.
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
        # otherwise generate a fresh uuid4. Untrusted values are never echoed
        # or logged verbatim.
        supplied = request.headers.get("X-Request-ID")
        request_id = supplied if supplied and _REQUEST_ID_RE.match(supplied) else str(uuid.uuid4())

        # Step 3 — bind to contextvars (NOT headers — headers contain JWTs).
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            request_path=request.url.path,
            request_method=request.method,
        )

        # Step 4 — hand off to the next handler.
        response: Response = await call_next(request)

        # Step 5 — echo the request ID back to the caller.
        response.headers["X-Request-ID"] = request_id

        # Step 6 — clean up so the next request starts fresh.
        structlog.contextvars.clear_contextvars()

        return response
