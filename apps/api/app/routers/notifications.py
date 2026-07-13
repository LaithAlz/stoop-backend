"""``POST /v1/notifications/{id}/ack`` + ``GET``/``POST /ack/{token}`` —
the emergency escalation chain's three acknowledgment surfaces (#108):
press-1 (``app/routers/webhooks/twilio.py``'s ``/voice`` handler), SMS
link tap (this file's ``POST /ack/{token}``), and dashboard case-open
(this file's ``POST /v1/notifications/{id}/ack``). All three ultimately
call ``app.agent.emergency_chain.acknowledge_notification`` /
``acknowledge_by_token`` — the SAME idempotent primitive every surface
shares (see that module's own docstring).

Shapes match ``docs/03-engineering/api-contracts.md``'s "Notifications /
emergencies" section (amended 2026-07-12 — see that doc for the full
GET/POST split rationale summarized below).

Safety review, 2026-07-12 (finding 1, CRITICAL) — GET /ack/{token} is
SIDE-EFFECT-FREE
--------------------------------------------------------------------------
An earlier revision acknowledged the notification directly on the ``GET``
request. That is a real, exploitable hole: SMS link-preview prefetchers
(iMessage/RCS rich links, some carrier spam scanners) issue a ``GET`` on
any URL in a text message to generate a preview, with NO human ever
tapping anything — which would silently stamp ``acknowledged_at`` and
stop a LIVE emergency chain before the landlord/backup contact ever saw
the message. Fixed by splitting the single endpoint into two:

- ``GET /ack/{token}`` — renders a minimal HTML confirmation page
  (``Cache-Control: no-store``, so no intermediary caches a stale
  acknowledged/not-acknowledged state) with a button that submits a
  ``POST`` to the SAME path. Calls ONLY
  ``emergency_chain.resolve_ack_token`` (read-only) — structurally
  cannot mutate anything, regardless of who or what issues the request.
- ``POST /ack/{token}`` — the ONLY code path that actually acknowledges
  via a token. Only ever reached by a genuine form submission (a real
  browser executing a user tap), never by a passive link-preview fetch.

Authorization
-------------
``POST /v1/notifications/{id}/ack`` is landlord-authenticated
(``require_landlord``) — the "opening the case in the dashboard" ack
surface. A future case-detail page (Train 2, not yet built) calling this
same endpoint when a landlord opens a case with a pending emergency
notification is the intended wiring; this endpoint IS that primitive,
built here so the surface exists the moment the dashboard needs it — no
dashboard UI is built by this issue.

Before delegating to the shared (admin-session) acknowledge primitive,
this handler FIRST re-selects the notification on the CALLER'S OWN
RLS-scoped session (``require_landlord``), **explicitly filtered by
`landlord_id` in the SQL itself** (safety review, 2026-07-12, spec finding
S2 — BLOCKING): CLAUDE.md's "RLS arrives in #22, code behaves as if it's
already on" rule means this query must be correct EVEN IF the RLS policy
were somehow not enforced on this connection — which is the ACTUAL
default state today, since ``get_session``/``require_landlord`` fall back
to the ADMIN engine (bypassing RLS entirely) until the one-time
``APP_DATABASE_URL`` operator step lands (``app/db/session.py``'s module
docstring). Relying on RLS ALONE here — the previous revision's
``SELECT id FROM notifications WHERE id = :id`` — would let ANY
authenticated landlord silence ANY OTHER landlord's emergency chain by id
in every environment that hasn't done that one-time step (which is every
environment today). The explicit ``AND landlord_id = :landlord_id``
predicate makes this endpoint's own authorization correct independent of
whether RLS is enforced on the connection — defense in depth, not a
replacement for RLS (RLS still isolates every OTHER query this session
might run).

``GET``/``POST /ack/{token}`` are INTENTIONALLY public (no
``Authorization`` header required): the tokenized link is texted directly
to the landlord/backup contact by the escalation chain itself
(``emergency_chain.render_ack_url``) — possession of that unguessable,
random token (``secrets.token_urlsafe(24)``, ~144 bits of entropy) IS the
authorization, exactly like a password-reset link. ``POST`` returns the
same JSON shape as the dashboard endpoint per api-contracts.md — a nicer
human-facing "thanks, acknowledged" HTML response after submission is a
UX enhancement for whichever issue builds the tenant/landlord-facing web
surfaces, out of this backend issue's scope.

Rate limiting (safety review, 2026-07-12, finding 8, LOW) — throttled
ack FAILS SAFE
--------------------------------------------------------------------------
A modest, in-memory, per-token fixed-window limit
(:data:`_RATE_LIMIT_MAX_REQUESTS` per :data:`_RATE_LIMIT_WINDOW_SECONDS`)
guards both ``/ack/{token}`` surfaces against a naive scripted hammer of
one specific link. "Fails safe" here means the limiter can ONLY ever
reject an individual HTTP request with 429 — it has no way to reach into,
pause, or gate the escalation chain itself (that machinery lives entirely
in ``notifications`` rows and the scheduler ticker, both untouched by this
router); the landlord/backup contact keep getting called and texted on
schedule regardless of whether their own ack attempts are being throttled.
Deliberately NOT a distributed/durable limiter (no new dependency, no new
table) — v1 scale is a handful of concurrent emergencies at most; an
in-memory, best-effort, per-process counter is proportionate. Any
internal error in the limiter itself is treated as "not rate limited"
(never blocks a genuine ack due to a bug in the limiter) — the SAME
fail-safe direction as everywhere else in this module.
"""

from __future__ import annotations

import html
import time
from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import emergency_chain
from app.deps import Landlord, require_landlord
from app.errors import AppError

log = structlog.get_logger(__name__)

router = APIRouter(tags=["notifications"])

_NOTIFICATION_NOT_FOUND_CODE = "notification_not_found"
_NOTIFICATION_NOT_FOUND_MESSAGE = "No notification found."
_RATE_LIMITED_CODE = "rate_limited"
_RATE_LIMITED_MESSAGE = "Too many attempts — please wait a moment and try again."

_RATE_LIMIT_MAX_REQUESTS = 20
_RATE_LIMIT_WINDOW_SECONDS = 60.0
# Crude, cheap memory bound: an attacker hitting many DIFFERENT (mostly
# nonexistent) token paths could otherwise grow this dict unboundedly. At
# this size a full reset (every tracked count returns to zero) is a
# negligible, occasional cost, not a real DoS vector, and is simpler than
# a proper LRU for a LOW-priority, defense-in-depth feature.
_RATE_LIMIT_MAX_TRACKED_TOKENS = 5_000

_rate_limit_state: dict[str, tuple[int, float]] = {}


def _is_rate_limited(token: str) -> bool:
    """``True`` if *token* has exceeded the modest per-token rate limit —
    see module docstring "Rate limiting". Fails safe: any internal error
    is treated as "not rate limited"."""
    try:
        now = time.monotonic()
        if len(_rate_limit_state) > _RATE_LIMIT_MAX_TRACKED_TOKENS:
            _rate_limit_state.clear()

        count, window_start = _rate_limit_state.get(token, (0, now))
        if now - window_start > _RATE_LIMIT_WINDOW_SECONDS:
            count, window_start = 0, now
        count += 1
        _rate_limit_state[token] = (count, window_start)
        return count > _RATE_LIMIT_MAX_REQUESTS
    except Exception as exc:  # pragma: no cover — defensive: never blocks a genuine ack
        log.error("ack_rate_limiter_failed", exc_type=type(exc).__name__)
        return False


def reset_rate_limiter_for_tests() -> None:
    """Test-only seam — mirrors the module-level-state reset convention
    used throughout this codebase (e.g. ``app/scheduler.py``'s
    ``reset_scheduler_for_tests``)."""
    _rate_limit_state.clear()


# Safety review, 2026-07-12 (spec finding S2, BLOCKING): landlord_id is a
# bind parameter in the WHERE clause itself, not left to RLS alone -- see
# module docstring "Authorization".
_SELECT_NOTIFICATION_OWNED_SQL = text(
    "SELECT id FROM notifications WHERE id = :id AND landlord_id = :landlord_id"
)

_NO_STORE_HEADERS = {"Cache-Control": "no-store"}

_ACK_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stoop</title></head>
<body style="font-family: sans-serif; max-width: 26rem; margin: 3rem auto; \
text-align: center; padding: 0 1rem;">
<h1>{heading}</h1>
<p>{body}</p>
<form method="POST" action="/ack/{token}">
<button type="submit" style="font-size: 1.1rem; padding: 0.75rem 1.5rem;">Acknowledge</button>
</form>
</body></html>
"""

_ACK_NOT_FOUND_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stoop</title></head>
<body style="font-family: sans-serif; max-width: 26rem; margin: 3rem auto; \
text-align: center; padding: 0 1rem;">
<h1>Link not found</h1>
<p>This acknowledgment link isn't valid.</p>
</body></html>
"""

_ACK_RATE_LIMITED_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stoop</title></head>
<body style="font-family: sans-serif; max-width: 26rem; margin: 3rem auto; \
text-align: center; padding: 0 1rem;">
<h1>Too many attempts</h1>
<p>Please wait a moment and try again.</p>
</body></html>
"""


class AckResponse(BaseModel):
    """``{"acknowledged_at": "…"}`` — api-contracts.md's ack response shape."""

    acknowledged_at: datetime


@router.post("/v1/notifications/{notification_id}/ack", response_model=AckResponse)
async def ack_notification(
    notification_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> AckResponse:
    """Dashboard/authenticated ack surface — see module docstring
    "Authorization". Idempotent: acking an already-acknowledged
    notification (e.g. two browser tabs) returns the SAME
    ``acknowledged_at``, never an error.

    Raises
    ------
    AppError
        404 ``notification_not_found`` if *notification_id* does not exist
        OR belongs to a different landlord (collapsed into one response —
        see module docstring "Authorization").
    """
    landlord, session = landlord_and_session

    owned = (
        (
            await session.execute(
                _SELECT_NOTIFICATION_OWNED_SQL,
                {"id": str(notification_id), "landlord_id": str(landlord.id)},
            )
        )
        .mappings()
        .one_or_none()
    )
    if owned is None:
        raise AppError(
            status_code=404,
            code=_NOTIFICATION_NOT_FOUND_CODE,
            message=_NOTIFICATION_NOT_FOUND_MESSAGE,
        )

    acknowledged_at = await emergency_chain.acknowledge_notification(
        notification_id, actor="landlord", channel="dashboard"
    )
    if acknowledged_at is None:  # pragma: no cover — invariant: just confirmed it exists above
        raise AppError(
            status_code=404,
            code=_NOTIFICATION_NOT_FOUND_CODE,
            message=_NOTIFICATION_NOT_FOUND_MESSAGE,
        )

    log.info("notification_acknowledged_via_dashboard", notification_id=str(notification_id))
    return AckResponse(acknowledged_at=acknowledged_at)


@router.get("/ack/{token}", response_class=HTMLResponse)
async def ack_page(token: str) -> HTMLResponse:
    """SIDE-EFFECT-FREE confirmation page — see module docstring "Safety
    review, 2026-07-12 (finding 1, CRITICAL)". Calls ONLY
    ``emergency_chain.resolve_ack_token`` (read-only); NEVER acknowledges.
    ``Cache-Control: no-store`` on every response.
    """
    if _is_rate_limited(token):
        log.warning("ack_page_rate_limited")
        return HTMLResponse(
            content=_ACK_RATE_LIMITED_TEMPLATE, status_code=429, headers=_NO_STORE_HEADERS
        )

    result = await emergency_chain.resolve_ack_token(token)
    if result is None:
        log.info("ack_page_rendered", found=False)
        return HTMLResponse(
            content=_ACK_NOT_FOUND_TEMPLATE, status_code=404, headers=_NO_STORE_HEADERS
        )

    notification_id, acknowledged_at = result
    log.info(
        "ack_page_rendered",
        found=True,
        notification_id=str(notification_id),
        already_acknowledged=acknowledged_at is not None,
    )
    if acknowledged_at is not None:
        heading = "Already acknowledged"
        body = "This emergency has already been acknowledged — no further action needed."
    else:
        heading = "Acknowledge this emergency"
        body = (
            "Tap the button below to let Stoop know you've got it. This stops the calls and texts."
        )

    # Safety review, 2026-07-12 (finding 4, LOW) -- html.escape() the token
    # before interpolating it into the page. Every token that reaches this
    # line matched a real row via emergency_chain.resolve_ack_token, and
    # every token this codebase ever GENERATES is secrets.token_urlsafe(24)
    # (url-safe base64: letters/digits/``-``/``_`` only -- never an HTML
    # metacharacter), so this is defense-in-depth against a raw path
    # parameter being interpolated into HTML at all, not a fix for a
    # presently-reachable XSS: never rely on "the value happens to be safe
    # today" as the only reason unescaped user-controlled input reaches a
    # template.
    page_html = _ACK_PAGE_TEMPLATE.format(
        heading=heading, body=body, token=html.escape(token, quote=True)
    )
    return HTMLResponse(content=page_html, status_code=200, headers=_NO_STORE_HEADERS)


@router.post("/ack/{token}", response_model=AckResponse)
async def ack_by_token(token: str) -> AckResponse:
    """The ONLY code path that acknowledges via a token — see module
    docstring "Safety review, 2026-07-12 (finding 1, CRITICAL)". Reached
    exclusively by the confirmation page's form submission (a genuine
    user tap), never by a passive GET/link-preview fetch.

    Raises
    ------
    AppError
        404 ``notification_not_found`` if *token* matches no notification.
        429 ``rate_limited`` if this token has been hit too many times in
        the current window — see module docstring "Rate limiting". Never
        affects the underlying chain itself, only this one HTTP request.
    """
    if _is_rate_limited(token):
        log.warning("ack_by_token_rate_limited")
        raise AppError(status_code=429, code=_RATE_LIMITED_CODE, message=_RATE_LIMITED_MESSAGE)

    result = await emergency_chain.acknowledge_by_token(token, channel="sms_link")
    if result is None:
        raise AppError(
            status_code=404,
            code=_NOTIFICATION_NOT_FOUND_CODE,
            message=_NOTIFICATION_NOT_FOUND_MESSAGE,
        )
    _notification_id, acknowledged_at = result
    return AckResponse(acknowledged_at=acknowledged_at)


__all__: list[str] = ["reset_rate_limiter_for_tests", "router"]
