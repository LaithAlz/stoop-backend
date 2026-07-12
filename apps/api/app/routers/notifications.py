"""``POST /v1/notifications/{id}/ack`` + ``GET /ack/{token}`` — the
emergency escalation chain's three acknowledgment surfaces (#108):
press-1 (``app/routers/webhooks/twilio.py``'s ``/voice`` handler), SMS
link tap (this file's ``GET``), and dashboard case-open (this file's
``POST``). All three ultimately call
``app.agent.emergency_chain.acknowledge_notification`` /
``acknowledge_by_token`` — the SAME idempotent primitive every surface
shares (see that module's own docstring).

Shapes match ``docs/03-engineering/api-contracts.md``'s "Notifications /
emergencies" section exactly: ``POST /v1/notifications/{id}/ack`` → 200
``{"acknowledged_at": "…"}`` (also reachable via tokenized
``GET /ack/{token}``).

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
RLS-scoped session (``require_landlord``) — migration 0005's RLS policy on
``notifications`` means this SELECT returns nothing for a notification
belonging to a DIFFERENT landlord, collapsing "doesn't exist" and "isn't
yours" into the same 404 (same pattern ``require_landlord`` itself already
uses for ``account_deleted``). Skipping this check would let any
authenticated landlord silence ANY other landlord's emergency chain by id
— a real authorization hole the admin-session-based shared primitive
cannot catch on its own (it has no landlord identity to check against).

``GET /ack/{token}`` is INTENTIONALLY public (no ``Authorization`` header
required): the tokenized link is texted directly to the landlord/backup
contact by the escalation chain itself
(``emergency_chain.render_ack_url``) — possession of that unguessable,
random token (``secrets.token_urlsafe(24)``, ~144 bits of entropy) IS the
authorization, exactly like a password-reset link. Returns the same JSON
shape as the POST endpoint per api-contracts.md — a nicer human-facing HTML
confirmation page is a UX enhancement for whichever issue builds the
tenant/landlord-facing web surfaces, out of this backend issue's scope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
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

_SELECT_NOTIFICATION_OWNED_SQL = text("SELECT id FROM notifications WHERE id = :id")


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
        OR belongs to a different landlord (RLS collapses these into one
        response — see module docstring).
    """
    _landlord, session = landlord_and_session

    owned = (
        (await session.execute(_SELECT_NOTIFICATION_OWNED_SQL, {"id": str(notification_id)}))
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


@router.get("/ack/{token}", response_model=AckResponse)
async def ack_by_token(token: str) -> AckResponse:
    """Tokenized SMS-link ack surface — see module docstring
    "Authorization". Public by design (see there for why).

    Raises
    ------
    AppError
        404 ``notification_not_found`` if *token* matches no notification.
    """
    result = await emergency_chain.acknowledge_by_token(token, channel="sms_link")
    if result is None:
        raise AppError(
            status_code=404,
            code=_NOTIFICATION_NOT_FOUND_CODE,
            message=_NOTIFICATION_NOT_FOUND_MESSAGE,
        )
    _notification_id, acknowledged_at = result
    return AckResponse(acknowledged_at=acknowledged_at)


__all__: list[str] = ["router"]
