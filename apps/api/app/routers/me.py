"""GET /v1/me — authenticated landlord profile with lazy get-or-create.

Design notes
------------
A GET with a lazy-create side effect is intentional and well-precedented
(Stripe ``/v1/customers/me``, GitHub ``/user``).  The first call for a new
auth user upserts a ``landlords`` row keyed by ``auth_user_id`` (the JWT
``sub``); subsequent calls return the same row.  We return 200 on first-call
create rather than 201 to keep the client-side contract simple: the caller
always GETs a profile, never has to branch on 200 vs 201.

Race safety
-----------
Concurrent first-time requests (e.g. mobile + web opening at the same
instant) both execute:

    INSERT INTO landlords … ON CONFLICT (auth_user_id) DO UPDATE …

The ``ON CONFLICT`` clause means that at most one INSERT wins; the other
becomes an UPDATE.  Both return the same ``id`` via ``RETURNING``.  No
duplicate-key error ever surfaces to the caller.

Email sync
----------
Every call refreshes ``email`` from the verified JWT claim.  If the user
changes their email address in Supabase Auth, the next ``GET /v1/me`` picks
it up automatically without any explicit sync job.

``full_name`` uses ``COALESCE(EXCLUDED.full_name, landlords.full_name)`` so
that a token with no ``user_metadata.full_name`` does not overwrite a name
that was already stored.

PII / logging discipline
------------------------
``auth_user_id`` (a UUID) is bound to the structlog context for correlation.
Email and full_name are never logged — never-break rule #5.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
import structlog.contextvars
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.deps import require_user
from app.integrations.supabase_auth import AuthUser

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["me"])

# ---------------------------------------------------------------------------
# Response model — authoritative shape from api-contracts.md
# ---------------------------------------------------------------------------


class MeResponse(BaseModel):
    """Authenticated landlord profile.

    Fields match the ``GET /v1/me`` shape in ``docs/03-engineering/api-contracts.md``
    exactly.  Internal-only columns (``auth_user_id``, ``phone``,
    ``stripe_customer_id``, ``deleted_at``, ``updated_at``) are intentionally
    excluded.
    """

    id: UUID
    email: str | None
    full_name: str | None
    timezone: str
    voice_profile: dict[str, Any] | None
    price_cohort: str
    subscription_tier: str
    subscription_status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Upsert SQL — race-safe, single statement
# ---------------------------------------------------------------------------

_UPSERT_SQL = text(
    """
    INSERT INTO landlords (auth_user_id, email, full_name)
    VALUES (:auth_user_id, :email, :full_name)
    ON CONFLICT (auth_user_id) DO UPDATE
      SET email      = EXCLUDED.email,
          full_name  = COALESCE(EXCLUDED.full_name, landlords.full_name),
          updated_at = now()
    RETURNING id, email, full_name, timezone, voice_profile, price_cohort,
              subscription_tier, subscription_status, created_at
    """
)

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/me", response_model=MeResponse)
async def get_me(
    user: Annotated[AuthUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeResponse:
    """Return the authenticated landlord's profile, creating it on first call.

    ``auth_user_id`` is the JWT ``sub`` UUID.  On first access the
    ``landlords`` row is created; on subsequent accesses the existing row is
    returned (with email refreshed from the current JWT claim).

    Returns 200 regardless of whether the row was just created or already
    existed — the caller always receives a profile, never a 201 redirect.
    """
    # Bind the UUID to the structlog context so downstream log lines are
    # correlated.  We log the UUID only — never email, full_name, or the token.
    structlog.contextvars.bind_contextvars(auth_user_id=str(user.user_id))

    result = await session.execute(
        _UPSERT_SQL,
        {
            "auth_user_id": str(user.user_id),
            "email": user.email,
            "full_name": user.full_name,
        },
    )
    row = result.mappings().one()

    log.info("me_upserted", landlord_id=str(row["id"]))

    return MeResponse(
        id=row["id"],
        email=row["email"],
        full_name=row["full_name"],
        timezone=row["timezone"],
        voice_profile=row["voice_profile"],
        price_cohort=row["price_cohort"],
        subscription_tier=row["subscription_tier"],
        subscription_status=row["subscription_status"],
        created_at=row["created_at"],
    )
