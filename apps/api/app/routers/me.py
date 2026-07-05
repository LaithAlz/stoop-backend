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

Email-required guard (issue #135 part 2)
-----------------------------------------
``landlords.email`` is ``NOT NULL`` (schema-v1.md) but the JWT ``email``
claim is optional (``AuthUser.email: str | None`` — e.g. a phone-only
Supabase signup, or an existing landlord whose token loses the claim).
GoTrue also has no ``omitempty`` on that claim, so a real phone-only signup
emits ``"email": ""`` (present, empty) rather than an absent claim;
``supabase_auth.verify_jwt`` normalizes that (and whitespace-only / non-
string values) to ``None`` at the verification boundary, and this handler's
``if not user.email`` check is belt-and-suspenders on top of it. Before any
DB call, a verified identity with no usable email is rejected with a 403
``email_required`` via the standard error envelope (``app.errors.
AppError``) — fail-closed, nothing is written, and the ``landlords.email``
NOT-NULL invariant is preserved instead of surfacing as a 500
``NotNullViolation`` or (worse) silently overwriting an existing landlord's
real email with an empty string via the ``ON CONFLICT ... SET email =
EXCLUDED.email`` upsert.

Session: ``get_admin_session``, deliberately not ``get_session``/
``require_landlord`` (#22 safety review, BLOCKING item)
------------------------------------------------------------------------
This upsert MUST run on the admin engine, unscoped by RLS. Empirically
reproduced: under an RLS-scoped (``app_role``) session, BOTH the
brand-new-row INSERT and the existing-row ``ON CONFLICT DO UPDATE`` are
rejected by ``landlords``' ``WITH CHECK`` policy — a freshly
``gen_random_uuid()``'d ``id`` can never equal a GUC value that would have
to be set BEFORE that id exists, and ``require_landlord`` itself can't run
here for the same reason (it looks up the ``landlords`` row this endpoint
is the one that creates). This is exactly why ``GET /v1/me`` uses
``require_user`` (not ``require_landlord``) for auth AND
``get_admin_session`` (not ``get_session``) for its session — provisioning
is pre-identity and deliberately unscoped. Every OTHER, landlord-scoped
endpoint (#53 onward) must use ``require_landlord`` + the ordinary request
-path session instead — see ``app/db/session.py``'s module docstring.
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

from app.db.session import get_admin_session
from app.deps import require_user
from app.errors import AppError
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
    session: Annotated[AsyncSession, Depends(get_admin_session)],
) -> MeResponse:
    """Return the authenticated landlord's profile, creating it on first call.

    ``auth_user_id`` is the JWT ``sub`` UUID.  On first access the
    ``landlords`` row is created; on subsequent accesses the existing row is
    returned (with email refreshed from the current JWT claim).

    Returns 200 regardless of whether the row was just created or already
    existed — the caller always receives a profile, never a 201 redirect.

    Raises
    ------
    AppError
        403 ``email_required`` if the verified identity has no ``email``
        claim — checked BEFORE any DB call (see module docstring). Applies
        equally to a brand-new phone-only signup and to an existing
        landlord row whose current token has lost the email claim.
    """
    # Bind the UUID to the structlog context so downstream log lines are
    # correlated.  We log the UUID only — never email, full_name, or the token.
    structlog.contextvars.bind_contextvars(auth_user_id=str(user.user_id))

    if not user.email:
        # Fail-closed, before any DB call: `landlords.email` is NOT NULL but
        # this verified token carries no usable email claim (phone-only
        # signup, or an existing landlord whose token lost the claim).
        # `verify_jwt` already normalizes empty/whitespace/non-string email
        # claims to None (issue #135 safety review); `not user.email` here
        # is belt-and-suspenders against that normalization, not a
        # substitute for it. Nothing is written; the message is generic and
        # contains no token material.
        raise AppError(
            status_code=403,
            code="email_required",
            message="An email address is required to use the dashboard.",
        )

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
