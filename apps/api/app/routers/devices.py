"""Push-notification device registration (#210 M3) —
``docs/03-engineering/api-contracts.md``'s "Devices" section (v1.18
amendment).

Landlord-scoped via ``Depends(require_landlord)`` — same convention as
every other v1 resource router (see ``app/routers/vendors.py``'s module
docstring for the RLS + explicit-``landlord_id`` rationale).

Reuses ``schema-v1.md``'s existing ``push_tokens`` table (never invents a
``device_tokens`` one — see that doc's v1.13 amendments for the full
reasoning) and its ``token``/``platform`` column names verbatim; there is
no ``expo_push_token`` field at either the schema or the API layer.

Never logs a push token (credential-adjacent, rule #5-adjacent) — every
``log.*`` call in this module carries only uuids.

Push is for approvals/status only — this router has no relationship to
the emergency escalation chain (``app/agent/emergency_chain.py``) and
never touches, delays, or conditions it (CLAUDE.md rule #1). No feature
flags anywhere near it (rule #7). No rate limiting (auth'd, idempotent
upsert — #210's own instruction).

``DeviceRegisterRequest``'s Pydantic validation failures (empty/whitespace
``token``, an out-of-vocabulary ``platform``) 422 via the global
``RequestValidationError`` handler registered in ``app/main.py``
(``app/main.py``'s ``_validation_error_handler`` — issue #219), which
returns this codebase's house error envelope
(``{"error": {"code": "invalid_request", "message", "request_id"}}``,
``app/errors.py``) — no router-specific handling needed here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Landlord, require_landlord
from app.errors import AppError

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["devices"])

Platform = Literal["ios", "android"]
"""Narrower than ``push_tokens.platform``'s stored CHECK
(``'ios','android','web'``) — Expo push tokens have no ``'web'`` concept
(see schema-v1.md's v1.13 amendments); the mobile app registering here
only ever sends one of these two."""


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DeviceRegisterRequest(BaseModel):
    token: str = Field(min_length=1)
    platform: Platform

    @field_validator("token")
    @classmethod
    def _reject_whitespace_only(cls, value: str) -> str:
        """Mirrors ``app/routers/drafts.py``'s ``EditAndSendRequest.body``
        convention: ``Field(min_length=1)`` alone lets a whitespace-only
        string through."""
        if not value.strip():
            raise ValueError("token must not be empty or whitespace-only")
        return value


class DeviceResponse(BaseModel):
    id: UUID
    platform: Platform
    created_at: datetime


class DeviceDeleteResponse(BaseModel):
    status: Literal["deleted"]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Token ownership model (schema-v1.md v1.13 amendments): a token belongs to
# whoever registered it LAST. ON CONFLICT (token) -- push_tokens.token is
# UNIQUE -- moves the row to the calling landlord and clears revoked_at (a
# token the sweep previously marked dead is trusted again the instant a
# real registration call proves it live once more).
_UPSERT_DEVICE_SQL = text(
    """
    INSERT INTO push_tokens (landlord_id, token, platform)
    VALUES (:landlord_id, :token, :platform)
    ON CONFLICT (token) DO UPDATE
    SET landlord_id = EXCLUDED.landlord_id,
        platform = EXCLUDED.platform,
        last_seen_at = now(),
        revoked_at = NULL
    RETURNING id, platform, created_at
    """
)

# Explicit landlord_id predicate in the DELETE itself, never RLS alone --
# same "correct even if RLS isn't enforced on this connection" discipline
# as every other cross-tenant-sensitive query in this codebase (e.g.
# app/routers/notifications.py's _SELECT_NOTIFICATION_OWNED_SQL).
_DELETE_DEVICE_SQL = text(
    "DELETE FROM push_tokens WHERE id = :id AND landlord_id = :landlord_id RETURNING id"
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/devices", response_model=DeviceResponse, status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> DeviceResponse:
    """Upsert on ``token`` — see module docstring "Token ownership model".
    Re-registering the SAME token under the SAME landlord (e.g. app
    relaunch) is a no-op upsert, never a 409."""
    landlord, session = landlord_and_session

    row = (
        (
            await session.execute(
                _UPSERT_DEVICE_SQL,
                {
                    "landlord_id": str(landlord.id),
                    "token": body.token,
                    "platform": body.platform,
                },
            )
        )
        .mappings()
        .one()
    )
    log.info("device_registered", device_id=str(row["id"]), platform=row["platform"])
    return DeviceResponse(id=row["id"], platform=row["platform"], created_at=row["created_at"])


@router.delete("/devices/{device_id}", response_model=DeviceDeleteResponse)
async def unregister_device(
    device_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> DeviceDeleteResponse:
    """Hard delete — the explicit sign-out/unregister surface. See
    api-contracts.md's Devices section for why this is NOT idempotent-200
    on repeat (unlike ``push_tokens.revoked_at``, which is a soft marker
    the push sweep sets on a dead token — see schema-v1.md's v1.13
    amendments).

    Raises
    ------
    AppError
        404 ``device_not_found`` if *device_id* does not exist OR belongs
        to a different landlord (collapsed into one response — same
        non-disclosure convention as every other ``/v1/{resource}/{id}``
        404 in this codebase).
    """
    landlord, session = landlord_and_session

    row = (
        (
            await session.execute(
                _DELETE_DEVICE_SQL, {"id": str(device_id), "landlord_id": str(landlord.id)}
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(status_code=404, code="device_not_found", message="Device not found.")

    log.info("device_unregistered", device_id=str(device_id))
    return DeviceDeleteResponse(status="deleted")


__all__: list[str] = ["router"]
