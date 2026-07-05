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
import os
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
    """Deny-by-default scrubber: drop every event field that can carry app data.

    Defense-in-depth on top of ``send_default_pii=False`` and
    ``include_local_variables=False``. Those flags are necessary but not
    sufficient — several carriers ship regardless of them, and Stoop never
    needs any of them in Sentry (never-break rule #5 — never send a JWT,
    phone number, or message body to a third party):

    - ``request`` — headers/cookies hold the Authorization JWT and session
      cookie; ``data`` holds the request body (message text); ``query_string``
      is attached by the ASGI integration *independently* of
      ``send_default_pii`` and can hold a token or phone. We drop the whole
      block; the ``transaction`` (route pattern) is enough to locate an error.
    - ``breadcrumbs`` — the logging integration turns prior stdlib log lines
      into breadcrumbs; we never want those leaving the box.
    - ``extra`` / ``logentry`` — app-attached log context and the captured
      log message+params.
    - stack-frame ``vars`` — belt-and-suspenders even with
      ``include_local_variables=False``.

    The exception type/value and stack structure are kept so errors remain
    actionable. (A ``raise ValueError(phone)`` would still carry PII in the
    value — that is an application bug rule #5 forbids at the source, not
    something this generic scrubber can detect.)
    """
    for key in ("request", "breadcrumbs", "extra", "logentry"):
        event.pop(key, None)  # type: ignore[misc]

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
    from sentry_sdk.integrations.logging import LoggingIntegration
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
            # configure_logging() bridges ALL stdlib logging through structlog.
            # The default LoggingIntegration would turn every log record into a
            # breadcrumb and capture ERROR+ records as events — a back door for
            # PII/message bodies into Sentry. Disable both behaviours (level and
            # event_level = None); we capture exceptions explicitly via the ASGI
            # integration instead. See never-break rule #5.
            LoggingIntegration(level=None, event_level=None),
        ],
        traces_sample_rate=0.1 if settings.is_production else 1.0,
    )


def init_langsmith_tracing() -> None:
    """Export the ``LANGSMITH_*`` env vars the ``langsmith``/LangChain/
    LangGraph SDKs read ambiently, if (and only if) a LangSmith API key is
    configured (#26).

    Deliberately a no-op when ``settings.langsmith_api_key`` is ``None`` —
    there is no LangSmith account yet (see ``app/config.py``'s
    ``langsmith_api_key`` docstring) — mirroring ``init_sentry()``'s
    "absence of credentials must never break startup" pattern exactly.
    When unset, NOT ONE of the ``LANGSMITH_*`` env vars below is ever
    exported, so the ``langsmith`` SDK's own ambient env-var detection
    (``langsmith.utils.tracing_is_enabled``) finds nothing and tracing
    stays fully inert — no network calls, no import-time surprises.

    When set, this exports the three env vars the SDK reads directly
    (there is no Python-level "enable tracing" call to make instead — the
    ``langsmith``/``langchain-core`` SDKs are entirely env-var driven for
    this):
    - ``LANGSMITH_TRACING=true`` — the modern name for what used to be
      ``LANGCHAIN_TRACING_V2`` (the SDK still recognizes the old name; we
      only ever set the current one).
    - ``LANGSMITH_API_KEY`` — never logged (never-break rule #5 concerns
      JWTs/phone numbers/message bodies specifically, but the same
      "secrets are never logged" discipline applies to every credential
      this app holds).
    - ``LANGSMITH_PROJECT`` — only set when ``settings.langsmith_project``
      is itself configured; otherwise the SDK falls back to its own
      "default" project, exactly as if this line were never run.
    """
    if not settings.langsmith_api_key:
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    if settings.langsmith_project:
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
