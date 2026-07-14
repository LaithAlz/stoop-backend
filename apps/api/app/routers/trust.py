"""``POST /v1/properties/{id}/trust/revoke`` (#60) — the landlord-facing
side of the trust ladder's revocation hook.

Shape matches ``docs/03-engineering/api-contracts.md``'s "Trust ladder"
section (v1.13 amendment — no contract existed for this endpoint before
#60; doc-first amendment landed in the same change as this router).

Auth / scoping
--------------
``require_landlord`` (#22) — RLS-scoped session, plus an explicit
``landlord_id = :landlord_id`` predicate on every query (belt-and-braces,
matching every other landlord-scoped router in this codebase). A
``property_id`` that exists but belongs to a different landlord is
indistinguishable from one that doesn't exist at all — both 404
``property_not_found``, same non-disclosure convention
``app/routers/properties.py`` already established.

Scope: "property" vs "global"
------------------------------
``scope="property"`` (the default) revokes ONLY *property_id*'s
``'routine'`` autonomy — the one severity that can ever be unlocked
(schema-v1.md). ``scope="global"`` revokes EVERY currently-unlocked
``(property, severity)`` row across the CALLING LANDLORD's entire
portfolio (:func:`app.trust.revoke_all_autonomy` — the same function a
future automated misclassification signal, #66-70, would call). Both
branches write through ``app/trust.py`` — this router never touches
``trust_metrics``/``audit_log`` directly, so there is exactly one place in
the codebase that implements revoke semantics.

Idempotent, one action, always audited
-----------------------------------------
Calling this endpoint when there is nothing left to revoke (never
unlocked, or already revoked) still returns 200 with ``revoked_count: 0``
— never a 404/409 for "nothing to do". A ``trust_revoked`` ``audit_log``
row (``actor='landlord'``) is appended on EVERY successful call regardless
of ``revoked_count`` — the landlord's action is real and worth recording
even when it had no effect (mirrors ``app/routers/drafts.py``'s own
``undo``, which records ``send_cancelled`` even on an idempotent repeat).
Re-graduation after a revoke starts ``consecutive_clean`` over at 0 (see
``app/trust.py``'s own "Re-graduation semantics" docstring) — earning
auto-send back requires a full fresh streak, not a single next clean send.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.trust import revoke_all_autonomy, revoke_property_autonomy

router = APIRouter(prefix="/v1", tags=["trust"])

_DEFAULT_REASON = "landlord_requested"

_SELECT_PROPERTY_EXISTS_SQL = text(
    "SELECT id FROM properties WHERE id = :property_id AND landlord_id = :landlord_id"
)


class RevokeTrustRequest(BaseModel):
    scope: Literal["property", "global"] = "property"
    reason: str | None = None


class RevokeTrustResponse(BaseModel):
    scope: Literal["property", "global"]
    revoked_count: int


async def _property_exists(session: AsyncSession, *, property_id: str, landlord_id: str) -> bool:
    row = (
        (
            await session.execute(
                _SELECT_PROPERTY_EXISTS_SQL,
                {"property_id": property_id, "landlord_id": landlord_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return row is not None


@router.post("/properties/{property_id}/trust/revoke", response_model=RevokeTrustResponse)
async def revoke_trust(
    property_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    body: RevokeTrustRequest | None = None,
) -> RevokeTrustResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    payload = body or RevokeTrustRequest()
    reason = payload.reason or _DEFAULT_REASON

    if not await _property_exists(session, property_id=str(property_id), landlord_id=landlord_id):
        raise AppError(status_code=404, code="property_not_found", message="Property not found.")

    if payload.scope == "global":
        count = await revoke_all_autonomy(
            session, landlord_id=landlord.id, actor="landlord", reason=reason
        )
    else:
        count = await revoke_property_autonomy(
            session,
            landlord_id=landlord.id,
            property_id=property_id,
            actor="landlord",
            reason=reason,
        )

    return RevokeTrustResponse(scope=payload.scope, revoked_count=count)


__all__: list[str] = ["router"]
