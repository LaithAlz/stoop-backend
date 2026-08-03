"""Stoop API — entry point.

Usage:
    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fastapi
import sentry_sdk
import structlog
import structlog.contextvars
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import settings
from app.db.session import verify_request_engine_role_separation
from app.errors import AppError
from app.integrations.supabase_auth import AuthError
from app.middleware.request_id import RequestIDMiddleware
from app.observability import configure_logging, init_langsmith_tracing, init_sentry
from app.routers import (
    cases,
    devices,
    drafts,
    health,
    me,
    notifications,
    properties,
    queue,
    tenants,
    trust,
    vendors,
)
from app.routers.webhooks import twilio as webhooks_twilio
from app.scheduler import start_scheduler, stop_scheduler

log = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(_app: fastapi.FastAPI) -> AsyncIterator[None]:
    """Startup self-checks (#22 safety review item 13b; #24).

    ``verify_request_engine_role_separation`` proves — not just assumes —
    that the request-path engine is genuinely isolated from the admin
    engine whenever ``APP_DATABASE_URL`` is set. Raising here (before
    ``yield``) aborts FastAPI/uvicorn startup entirely: refusing to serve
    any traffic is the correct failure mode for "RLS role separation was
    configured wrong," which is worse silently than not configuring it at
    all. See ``app/db/session.py``'s module docstring for the full
    rationale.

    ``setup_checkpointer()`` runs AFTER that check, unconditionally
    (#24) — it idempotently creates/migrates the LangGraph checkpoint
    tables in the dedicated ``langgraph`` schema (see
    ``app/agent/checkpointer.py``'s module docstring). Fail-closed: a
    failure here RAISES and aborts startup, same as the role-separation
    check above — the agent graph cannot run without checkpoint tables,
    so serving traffic with a broken checkpoint store is worse than not
    starting at all. Cheap to run on every process start even when the
    graph/Anthropic key goes unused this deploy.

    ``start_scheduler()`` runs LAST, after both checks above pass — the
    60-second ticker (``app/scheduler.py``, #108/#109) that drives the
    emergency escalation chain sweep and the degraded-mode retry sweep.
    Never raises (it only schedules an ``asyncio.Task``); shutdown
    symmetry stops it via ``stop_scheduler()`` before the checkpointer's
    pool closes, so no sweep tick is ever mid-flight against a
    just-closed connection pool.
    """
    await verify_request_engine_role_separation()
    await setup_checkpointer()
    start_scheduler()
    yield
    # Shutdown symmetry, reverse order: stop the scheduler first (no new
    # sweep ticks once this returns), then close the checkpointer's
    # dedicated psycopg pool so a graceful stop doesn't abandon open
    # sockets/worker tasks.
    await stop_scheduler()
    await close_checkpointer()


def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
    """Convert an ``AuthError`` into a 401 with the standard error envelope.

    Error envelope shape (api-contracts.md):
        {"error": {"code": "...", "message": "...", "request_id": "..."}}

    ``request_id`` is pulled from the structlog contextvar bound by
    ``RequestIDMiddleware`` — may be None if the middleware hasn't run yet
    (e.g. a test that hits the handler directly), which is acceptable.

    Security: the raw token NEVER appears in this response.  The ``message``
    is intentionally generic; only the ``code`` distinguishes error types.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            }
        },
    )


def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Convert an ``AppError`` into its declared status code + the standard envelope.

    Same envelope shape and ``request_id`` sourcing as ``_auth_error_handler``,
    but for business-rule (non-auth) failures that need a status other than
    401 — e.g. 403 ``email_required`` on ``GET /v1/me``.

    ``exc.extra`` (#44/#45 — e.g. ``fresh_draft_id`` on a 409 ``draft_stale``)
    is merged into the ``"error"`` object alongside the three standard keys.
    ``exc.extra`` is spread FIRST, then the three reserved keys — never the
    other way around (safety review, this round): a dict literal's later
    keys win on collision, so spreading ``extra`` last would let a caller
    accidentally override the statically-reviewed ``code``/``message``/
    ``request_id`` with whatever it happened to put in ``extra``. No
    current call site does this, but the ordering itself is the guarantee,
    not an audit of today's call sites.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                **exc.extra,
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
            }
        },
    )


def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Convert FastAPI's default ``RequestValidationError`` 422 into the
    standard error envelope (issue #219).

    Every Pydantic-validated request body/query/path param in this app
    (``drafts.py``'s ``EditAndSendRequest.body``, ``devices.py``'s
    ``DeviceRegisterRequest``, and any future validated router) raises this
    exception when validation fails. Left unhandled, FastAPI returns its
    OWN default body — ``{"detail": [{"loc": [...], "msg": "...", "input":
    ...}]}`` — which is neither the house envelope
    (``{"error": {"code","message","request_id"}}``, ``app/errors.py``) NOR
    safe: the ``"input"`` key echoes the submitted value verbatim, and for
    a validated endpoint that field can carry request-supplied PII (a
    tenant phone number, message-body text, a push token) straight into the
    HTTP response body — a direct violation of never-break rule #5.

    Security / PII-safety choice: this handler returns a single, static,
    generic ``message`` and status 422 with ``code: "invalid_request"`` —
    it NEVER forwards ``exc.errors()`` (or anything derived from it, e.g.
    the field ``loc`` paths) into the response. ``loc`` paths are almost
    always just static field names, but they can legally embed
    client-controlled data too (e.g. a dict/list key parsed from the
    request body) — omitting them entirely applies the same "never
    interpolate request data into a response" discipline ``app/errors.py``'s
    ``AppError.message`` already enforces, reviewed once, here, rather than
    re-auditing every current AND future validated request model for
    whether its ``loc`` shape could ever carry a client-supplied key. The
    generic message costs nothing: ``code`` is the stable, machine-readable
    signal ("check your request shape"); a landlord-facing client never
    surfaces this text directly anyway.

    Same ``request_id`` sourcing as ``_auth_error_handler``/
    ``_app_error_handler`` above (the structlog contextvars bound by
    ``RequestIDMiddleware``) — may be ``None`` if the middleware hasn't run
    yet (e.g. a test that calls this handler directly), which is
    acceptable.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "The request body or parameters failed validation.",
                "request_id": request_id,
            }
        },
    )


def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all ``500`` handler (issue #186 follow-up safety-review round,
    NEW-1) — converts ANY exception that reaches here (nothing more
    specific matched — see ``starlette._exception_handler._lookup_
    exception_handler``'s MRO walk: ``AuthError``/``AppError``/
    ``RequestValidationError`` registered above always win for their own
    exact types regardless of registration order) into the house error
    envelope instead of Starlette's own default response.

    Verified directly against this installed Starlette version: an
    unhandled exception with NO registered handler produces a plain
    ``"Internal Server Error"`` (``text/plain``, status 500) — never the
    house JSON envelope every OTHER error path in this codebase returns.
    ``app/agent/case_lock_pool.py``'s own docstring first named this gap
    concretely (the dashboard drafts endpoints, under a dedicated
    -lock-pool checkout timeout) — this handler closes it for every
    endpoint, not just that one call site. See api-contracts.md's
    Conventions v1.21 amendment (doc-first, same commit).

    Security (rule #5, mirrors ``_validation_error_handler``'s own
    precedent): ``message`` is a STATIC, generic string — NEVER derived
    from ``exc`` (its ``str()``/``args``/traceback could carry
    request-derived data from a poorly-scoped f-string raised somewhere
    deep in the stack; the discipline this codebase already applies to
    every ``AppError``/log line applies here too, enforced once, centrally,
    rather than re-auditing every possible unhandled exception site).
    Only the exception TYPE NAME (never its message or traceback) reaches
    Sentry or the local log — via an EXPLICIT ``sentry_sdk.capture_message``
    call, the same shape every other Sentry call in this codebase uses
    (never ``capture_exception`` — that would serialize the exception's own
    ``str()``/args, the same risk this handler's ``message`` field already
    avoids).

    Mechanism (CORRECTED — safety review, 2026-08-03, #251 F1; the
    previous version of this paragraph was wrong): registering a handler
    for ``Exception`` here does NOT make ``ExceptionMiddleware`` treat the
    exception as "handled" and stop it from ever reaching
    ``ServerErrorMiddleware``. Verified directly against the installed
    Starlette version (1.3.1): ``Starlette.build_middleware_stack``
    special-cases the ``Exception``/``500`` key — it is pulled OUT of the
    dict handed to ``ExceptionMiddleware`` and passed instead as
    ``ServerErrorMiddleware``'s own ``handler=`` kwarg.
    ``ServerErrorMiddleware`` is the OUTERMOST layer of the entire ASGI
    stack — above every ``add_middleware`` call in ``create_app`` below,
    including ``CORSMiddleware`` and ``RequestIDMiddleware`` (see
    ``create_app``'s own docstring, step "3b") — so THIS function IS what
    builds the actual 500 response, and ``ServerErrorMiddleware`` sends
    that response directly, bypassing both of those middlewares' own
    response-shaping logic entirely (their ``send()`` wrappers are never
    invoked for it). That is precisely why this handler sets
    ``Access-Control-Allow-Origin``/``Vary``/``X-Request-ID`` itself,
    below — nothing upstream adds them for this one response shape, the
    ONE error class where the dashboard most needs a readable response
    (a real outage). Sentry's own ``StarletteIntegration``/
    ``FastApiIntegration`` ordinarily hooks this same
    ``ServerErrorMiddleware`` boundary for automatic capture; paging
    explicitly via ``sentry_sdk.capture_message`` above removes any
    dependency on that automatic behavior still firing once a handler is
    installed, matching every OTHER failure path in this codebase
    (``app/agent/graph_entry.py``, ``app/routers/webhooks/twilio.py``,
    ...), none of which rely on Sentry's automatic capture either.
    """
    request_id: str | None = structlog.contextvars.get_contextvars().get("request_id")
    log.error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        path=request.url.path,
        request_id=request_id,
    )
    sentry_sdk.capture_message(
        "Unhandled exception reached the catch-all 500 handler",
        level="error",
        extras={"exc_type": type(exc).__name__, "request_id": request_id},
    )
    response = JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Something went wrong on our end. Please try again.",
                "request_id": request_id,
            }
        },
    )

    # #251 safety review, F1 (HIGH): this response is sent directly by
    # ServerErrorMiddleware, OUTSIDE CORSMiddleware/RequestIDMiddleware
    # (see this docstring's "Mechanism" section above) — without this,
    # every 500 from an allowed browser origin would arrive with no
    # Access-Control-Allow-Origin, get silently discarded by the
    # browser's own CORS enforcement, and surface to the landlord as a
    # generic network error instead of a readable "something's wrong"
    # message — the silence direction, during the one failure class
    # (a real outage) where it matters most. Origin-gated the exact same
    # way CORSMiddleware itself gates every other response: no Origin
    # header (webhooks, server-to-server calls) or a disallowed Origin ->
    # add nothing, preserving the same webhook invariance CORSMiddleware
    # guarantees elsewhere (app/config.py's dashboard_origins_list is the
    # single source of truth for "allowed", same as CORSMiddleware's own
    # allow_origins= below).
    origin = request.headers.get("origin")
    if origin is not None and origin in settings.dashboard_origins_list:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        if request_id is not None:
            response.headers["X-Request-ID"] = request_id
    return response


def create_app() -> fastapi.FastAPI:
    """App factory — returns a fully configured FastAPI application.

    Calling this at module level (``app = create_app()``) ensures the
    ASGI app exists at import time so uvicorn's ``app.main:app`` import
    works without any lazy initialisation.

    Startup order:
      1. configure_logging()        — structlog JSON setup
      2. init_sentry()              — no-op unless SENTRY_DSN is set
      2b. init_langsmith_tracing()  — no-op unless LANGSMITH_API_KEY is set
      3. add RequestIDMiddleware
      3b. add CORSMiddleware (#251) — origin allowlist from
          ``settings.dashboard_origins_list`` (env ``DASHBOARD_ORIGINS``,
          never ``'*'``). Added AFTER RequestIDMiddleware so it wraps
          OUTERMOST among the two ``add_middleware`` calls in this
          function: Starlette's ``add_middleware`` inserts each new entry
          at the front of ``user_middleware``, and
          ``build_middleware_stack`` wraps that list in reverse — so the
          LAST middleware added here wraps the other, running first on the
          way in and last on the way out. That covers every 2xx/4xx
          response — including 401/422 from the exception handlers below —
          CORS headers land on all of them via this middleware. It does
          NOT cover every response this process ever sends: a genuinely
          UNHANDLED exception is caught by ``ServerErrorMiddleware``,
          which Starlette installs OUTSIDE every ``add_middleware`` layer
          entirely — CORRECTED (safety review, 2026-08-03, #251 F1; a
          prior version of this note wrongly claimed 500s were covered
          too, verified false on the installed Starlette version, 1.3.1)
          — see ``_unhandled_exception_handler``'s own docstring for the
          full mechanism; that one response shape bypasses CORSMiddleware
          and sets its own Origin-gated CORS headers explicitly instead of
          inheriting them here. A request with no ``Origin`` header (every
          webhook and server-to-server call) passes straight through this
          middleware untouched — no header is added or removed, Twilio
          signature verification is unaffected (verified directly against
          the installed Starlette version's ``CORSMiddleware.__call__``).
          Preflight ``OPTIONS`` requests are answered entirely inside
          ``CORSMiddleware`` and never reach ``RequestIDMiddleware``/the
          router/auth — expected: that's the browser's own preflight
          check, not a real API call.
      4. register AuthError exception handler (401 → standard envelope)
      4b. register AppError exception handler (status_code → standard envelope)
      4c. register RequestValidationError exception handler (422 → standard
          envelope, code ``invalid_request`` — #219; supersedes every
          Pydantic-validated router's previous fallback onto FastAPI's own
          default 422 body)
      4d. register a catch-all Exception exception handler (500 → standard
          envelope, code ``internal_error`` — #186 follow-up round, NEW-1;
          api-contracts.md v1.21). Registered LAST among the four handlers,
          but registration order does not matter for correctness — Starlette
          resolves the MOST SPECIFIC registered handler for each exception's
          own MRO (see ``_unhandled_exception_handler``'s own docstring), so
          AuthError/AppError/RequestValidationError still take priority for
          their own exact types regardless of where this is added.
      5. include health router (always)
      5a. include properties/tenants/vendors/cases/queue routers (#54/#55/
          #56 — always, landlord-scoped via require_landlord), the drafts
          router (#44/#45 — the approve/reject/edit-and-send + undo
          endpoints; landlord-scoped via require_landlord), and the
          notifications router (always — POST /v1/notifications/{id}/ack is
          landlord-authenticated; GET /ack/{token} is the public
          tokenized-link ack surface, #108)
      5b. include Twilio webhook router (always — no auth header, its own
          signature verification; not gated by environment since Twilio
          must reach it in every deployment, including production)
      6. include auth-test router (always — for manual JWT verification)
      7. include debug router (non-production only)

    ``lifespan=_lifespan`` runs ``verify_request_engine_role_separation``,
    then ``setup_checkpointer()``, then ``start_scheduler()`` once at ASGI
    startup (#22 safety review item 13b; #24; #108/#109) — see that
    function's docstring for what each checks/starts and why. The
    role-separation check is a no-op when ``APP_DATABASE_URL`` is unset;
    checkpoint setup and the scheduler always run.
    """
    configure_logging()
    init_sentry()
    init_langsmith_tracing()

    application = fastapi.FastAPI(
        title="Stoop API",
        description="AI-powered tenant-maintenance handling for landlords.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    application.add_middleware(RequestIDMiddleware)

    # CORS (#251) — origin allowlist ONLY, never '*' (settings.py's
    # _reject_wildcard_dashboard_origin refuses to boot on a literal '*';
    # _validate_dashboard_origin_shapes and
    # _require_non_local_dashboard_origins_in_production, #251 safety
    # review F2, close the "boots clean, allowlist quietly matches
    # nothing" failure class too). Added AFTER RequestIDMiddleware so it
    # wraps OUTERMOST — see this function's own docstring, "3b", for the
    # full ordering rationale (including what it does NOT cover — an
    # unhandled 500, see _unhandled_exception_handler, #251 F1).
    # allow_credentials=False: this API authenticates via a bearer JWT
    # (Authorization header), never cookies, so there is nothing for
    # browser credential-mode CORS to protect here.
    #
    # F4 (#251 safety review): "X-Request-ID" is in allow_headers because
    # RequestIDMiddleware explicitly honors a client-supplied correlation
    # id (app/middleware/request_id.py) — without it here, a browser
    # preflight silently 400s the instant the dashboard ever sends that
    # header, while curl/httpx (no preflight) keep working fine. THE TRAP:
    # any FUTURE client-sent header (e.g. an Idempotency-Key) or method
    # (e.g. PUT) must be added to allow_headers/allow_methods HERE, or the
    # exact same silent-in-browsers-only failure recurs.
    #
    # F5 (#251 safety review, no code change — no cache exists today):
    # Starlette's CORSMiddleware only sends `Vary: Origin` on responses to
    # an ALLOWED origin (see its own `send()` — the header is set inside
    # `allow_explicit_origin`, never unconditionally). If a shared/HTTP
    # cache is ever put in front of this API, a disallowed-origin response
    # could be cached and replayed to an allowed origin without a `Vary`
    # signal telling the cache not to. Add an unconditional `Vary: Origin`
    # (or `Cache-Control: no-store` on `/v1/*`) at that point — not needed
    # while every response is generated fresh, per-request, as it is now.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.dashboard_origins_list,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["Date"],
        allow_credentials=False,
    )

    # F2c (#251 safety review): the allowlist itself is logged once at
    # boot — origins are non-sensitive (settings.py's dashboard_origins
    # field docstring), and this is the cheapest possible signal for "did
    # my DASHBOARD_ORIGINS actually take effect", catching a
    # copy-paste/env-var-name typo (e.g. a Fly secret set on the wrong
    # app) well before the first confused "the dashboard can't load"
    # report.
    log.info("cors_allowlist", origins=settings.dashboard_origins_list)

    # Register the AuthError handler so any router can raise AuthError and
    # get the standard 401 envelope without boilerplate.
    application.add_exception_handler(
        AuthError,
        _auth_error_handler,  # type: ignore[arg-type]
    )

    # Register the AppError handler so any router can raise AppError with an
    # arbitrary status code and get the standard envelope without boilerplate.
    application.add_exception_handler(
        AppError,
        _app_error_handler,  # type: ignore[arg-type]
    )

    # Register the RequestValidationError handler so every Pydantic
    # -validated request body/query/path param (drafts.py's
    # EditAndSendRequest.body, devices.py's DeviceRegisterRequest, and any
    # future validated router) gets the standard 422 envelope instead of
    # FastAPI's own default body, which can echo submitted request values
    # (#219). This handler ONLY reshapes RequestValidationError — it never
    # touches any other exception type, so AuthError/AppError's 401/
    # status_code envelopes above are unaffected.
    application.add_exception_handler(
        RequestValidationError,
        _validation_error_handler,  # type: ignore[arg-type]
    )

    # Register the catch-all Exception handler (#186 follow-up round,
    # NEW-1) so ANY unhandled exception on any endpoint gets the standard
    # 500 envelope instead of Starlette's own plain-text default — see
    # _unhandled_exception_handler's own docstring for why this is safe to
    # register alongside the three more specific handlers above (Starlette
    # always resolves the most specific match, never this one, for an
    # AuthError/AppError/RequestValidationError instance).
    application.add_exception_handler(
        Exception,
        _unhandled_exception_handler,
    )

    application.include_router(health.router)
    application.include_router(me.router)
    application.include_router(properties.router)
    application.include_router(tenants.router)
    application.include_router(vendors.router)
    application.include_router(cases.router)
    application.include_router(queue.router)
    application.include_router(drafts.router)
    application.include_router(notifications.router)
    application.include_router(trust.router)
    application.include_router(devices.router)
    application.include_router(webhooks_twilio.router)

    # auth-test: always registered so engineers can verify JWT plumbing with
    # real Supabase tokens in any environment.
    from app.routers import auth_test

    application.include_router(auth_test.router)

    if not settings.is_production:
        from app.routers import debug

        application.include_router(debug.router)

    return application


app: fastapi.FastAPI = create_app()
