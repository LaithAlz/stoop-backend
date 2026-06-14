"""Observability setup — structlog and Sentry.

Call ``configure_logging()`` and ``init_sentry()`` once at app startup
(inside ``create_app()`` in ``main.py``).

Safety rules enforced here:
- ``send_default_pii=False`` is MANDATORY on Sentry init to prevent request
  bodies / headers (including Authorization / JWTs) from being attached to
  events.
- We never log the settings object, JWTs, phone numbers, or message bodies.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint


def configure_logging() -> None:
    """Configure structlog for JSON output to stdout.

    Processor chain (per issue #7 spec):
      1. merge_contextvars   — pull in bound context (request_id, etc.)
      2. add_log_level       — add the ``level`` key
      3. TimeStamper(iso)    — add the ``timestamp`` key in ISO-8601
      4. StackInfoRenderer   — render stack_info if present
      5. format_exc_info     — render exc_info into the event dict
      6. JSONRenderer        — serialise to a single JSON line

    Also configures the stdlib ``logging`` root logger so that any library
    using stdlib logging (e.g. uvicorn, SQLAlchemy) is captured by structlog
    at the configured level.
    """
    log_level = getattr(logging, settings.log_level, logging.INFO)

    # Wire stdlib logging through structlog so every library's logs are
    # rendered as JSON too.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def _scrub_event(event: Event, _hint: Hint) -> Event:
    """Strip anything that could carry a JWT, phone number, or message body.

    Defense-in-depth on top of ``send_default_pii=False`` and
    ``include_local_variables=False``.  ``send_default_pii=False`` only
    filters the structured ``request.headers`` block; it does NOT stop
    Sentry from serialising stack-frame local variables, which hold the raw
    ASGI ``scope``/``request`` objects (and therefore the Authorization
    header / JWT).  We drop those here so a single re-enabled global can't
    silently start leaking tokens. See never-break rule #5.
    """
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("headers", "cookies", "data"):
            request.pop(key, None)

    for value in event.get("exception", {}).get("values", []):
        for frame in value.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)

    return event


def init_sentry(transport: Any | None = None) -> None:
    """Initialise the Sentry SDK if a DSN is configured.

    Deliberately a no-op when ``settings.sentry_dsn`` is ``None`` so that
    dev/test environments never connect to Sentry.

    ``transport`` lets tests inject an in-memory transport so the *real*
    protective configuration below is exercised without touching the
    network.  Production passes nothing, getting Sentry's default HTTP
    transport.

    CRITICAL (never-break rule #5 — preventing JWT/PII leakage):
    - ``send_default_pii=False`` keeps the structured request headers/body
      out of events.
    - ``include_local_variables=False`` stops Sentry serialising stack-frame
      locals — without this, an unhandled 500 on an authenticated route
      ships the caller's raw Authorization header (a live JWT) to Sentry.
    - ``before_send=_scrub_event`` is a belt-and-suspenders scrubber in case
      either flag is ever flipped back.
    None of these may be relaxed without a safety review.
    """
    if not settings.sentry_dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        transport=transport,
        send_default_pii=False,  # NEVER True — prevents JWT/body leakage
        include_local_variables=False,  # NEVER True — frame locals hold the JWT
        before_send=_scrub_event,
        integrations=[
            FastApiIntegration(),
            StarletteIntegration(),
        ],
        traces_sample_rate=0.1 if settings.is_production else 1.0,
    )
