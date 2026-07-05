"""FastAPI dependency providers.

``require_user`` is the gatekeeper for every authenticated endpoint.
Inject it with ``Depends(require_user)`` to receive a verified ``AuthUser``.

``require_landlord`` builds on ``require_user`` and is the building block
every RLS-scoped endpoint (#53-57 onward) will use — it resolves the
caller's ``landlords`` row and sets the ``app.current_landlord_id`` session
variable that migration 0005's RLS policies key off (#22). ``GET/PATCH
/v1/me`` deliberately keeps using ``require_user`` directly, not
``require_landlord`` — it is the provisioning path (the very first request
for a new auth user has no ``landlords`` row yet, and lazily creates one;
``require_landlord`` would 403 that same request instead of provisioning
it).

Usage::

    from app.deps import require_user, require_landlord
    from app.integrations.supabase_auth import AuthUser

    @router.get("/v1/example")
    async def example(user: AuthUser = Depends(require_user)) -> dict:
        return {"user_id": str(user.user_id)}

    @router.get("/v1/other-example")
    async def other_example(
        landlord_and_session: Annotated[
            tuple[Landlord, AsyncSession], Depends(require_landlord)
        ],
    ) -> dict:
        landlord, session = landlord_and_session
        ...  # every query on `session` is now scoped to `landlord.id` by RLS

Security:
- Parses the ``Authorization: Bearer <token>`` header.
- Delegates full JWT verification to ``supabase_auth.verify_jwt``.
- Any failure raises ``AuthError``; the registered exception handler
  converts it to a 401 with the standard error envelope.
- The raw token is NEVER logged, stored, or echoed in error messages.
- ``require_landlord`` never logs the landlord id's SET value beyond the
  existing ``auth_user_id``-only structlog convention already used by
  ``routers/me.py`` — no additional PII surface here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.errors import AppError
from app.integrations.supabase_auth import AuthError, AuthUser, verify_jwt

# Shared with ``routers/me.py``'s own soft-delete guard (issue #135 part 1)
# -- both call sites respond to the same underlying condition ("no live
# landlords row for this auth_user_id": soft-deleted, or never provisioned)
# and must present byte-identical wording. Declared here (where the
# ``account_deleted`` contract for the request-scoped surface is defined)
# and imported by ``me.py`` rather than re-declared there, so the two can
# never silently drift apart.
ACCOUNT_DELETED_CODE = "account_deleted"
ACCOUNT_DELETED_MESSAGE = "This account is no longer active."


async def require_user(request: Request) -> AuthUser:
    """Extract and verify the Supabase JWT from the ``Authorization`` header.

    Returns the verified ``AuthUser`` on success.

    Raises
    ------
    AuthError
        ``missing_token``  — no ``Authorization`` header, or not ``Bearer …``.
        ``invalid_token``  — bad signature, wrong iss/aud/alg/kid.
        ``expired``        — token was valid but has expired.
        ``forbidden_role`` — token is valid but has ``role: service_role``.
    """
    auth_header: str | None = request.headers.get("Authorization")

    if not auth_header:
        raise AuthError("missing_token", "Authentication required.")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():  # noqa: S105
        raise AuthError("missing_token", "Authentication required.")

    token = parts[1].strip()

    # verify_jwt raises AuthError on any failure — let it propagate.
    return await verify_jwt(token)


# ---------------------------------------------------------------------------
# require_landlord — the RLS-scoping building block (#22)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Landlord:
    """Minimal landlord identity resolved by ``require_landlord``.

    Deliberately just ``id`` — the one column every RLS-scoped endpoint
    needs to reference. Extend with more fields only when a real endpoint
    needs them (never invent a name not in ``schema-v1.md``); a handler
    that wants more can always ``SELECT`` the rest itself on the already
    -scoped ``session``.
    """

    id: UUID


_LANDLORD_LOOKUP_SQL = text(
    "SELECT id FROM landlords WHERE auth_user_id = :auth_user_id AND deleted_at IS NULL"
)

# `set_config(name, value, is_local)` rather than a bare
# `SET LOCAL app.current_landlord_id = '...'` string: this way the landlord
# id is a genuine bind parameter, never string-interpolated into SQL text —
# defense in depth even though the value always originates from our own
# `landlords.id` column, never raw client input.
_SET_CURRENT_LANDLORD_SQL = text("SELECT set_config('app.current_landlord_id', :landlord_id, true)")


async def require_landlord(
    user: Annotated[AuthUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> tuple[Landlord, AsyncSession]:
    """Resolve the caller's ``landlords`` row and scope ``session`` to it.

    Looks up the ``landlords`` row for ``user.user_id`` (excluding
    soft-deleted rows), then sets the ``app.current_landlord_id`` Postgres
    session variable that migration 0005's RLS policies key off — every
    subsequent query on the returned ``session`` is scoped to this landlord
    automatically, fail-closed if this were somehow skipped (an unset GUC
    reads back as ``NULL``, which matches zero rows under RLS).

    Returns
    -------
    ``(landlord, session)`` — the resolved ``Landlord`` and the same
    ``AsyncSession`` yielded by ``get_session``, now GUC-scoped.

    Raises
    ------
    AppError
        403 ``account_deleted`` if no live ``landlords`` row exists for this
        ``auth_user_id``. This collapses two cases into one response,
        deliberately: a soft-deleted landlord (``deleted_at`` set, #15's
        auth-lifecycle trigger) and a ``landlords`` row that doesn't exist
        at all yet for this ``auth_user_id``. Both mean "no live landlord
        row to scope this request against" — fail closed either way. In
        practice the second case shouldn't arise once a token reaches an
        endpoint behind ``require_landlord``: the row is provisioned either
        by the auth.users trigger (#15) or the lazy ``GET /v1/me`` upsert
        (#11) before any other endpoint is reachable, and #15's soft-delete
        is never a hard delete — so a missing row is not an expected
        steady state, just an edge this fails safely on instead of 500ing.

    Notes
    -----
    ``set_config(..., true)`` — the ``true`` is ``is_local``, i.e. ``SET
    LOCAL`` semantics: the setting is scoped to the CURRENT transaction
    only, discarded at COMMIT/ROLLBACK. This is load-bearing, not a style
    choice: Supabase's Supavisor pooler (transaction mode) can hand the
    *same* physical backend connection to a *different* logical session
    between transactions. A plain ``SET`` (session-level, ``is_local =
    false``) would leave ``app.current_landlord_id`` set on that physical
    connection after this request's transaction ends, and the next
    unrelated request that happens to reuse the same pooled backend would
    silently inherit this landlord's id. ``is_local = true`` guarantees the
    setting never outlives the transaction that ``get_session`` commits or
    rolls back at request teardown (``app/db/session.py``).

    WARNING for #53-57 authors: a mid-handler ``await session.commit()``
    ends the CURRENT transaction — and with it, this GUC (``SET LOCAL`` is
    scoped to the transaction, not the session; see above). Any query the
    same handler runs AFTER that commit executes on a new, unscoped
    transaction and fails closed to zero rows (matching ``NULL`` under
    RLS) — silent and confusing to debug, not an error. Do not call
    ``session.commit()`` inside a handler that used ``require_landlord``;
    ``get_session``'s teardown commit (``app/db/session.py``) is the only
    commit that should ever happen for that session.
    """
    result = await session.execute(_LANDLORD_LOOKUP_SQL, {"auth_user_id": str(user.user_id)})
    row = result.mappings().one_or_none()

    if row is None:
        raise AppError(
            status_code=403,
            code=ACCOUNT_DELETED_CODE,
            message=ACCOUNT_DELETED_MESSAGE,
        )

    landlord = Landlord(id=row["id"])

    await session.execute(_SET_CURRENT_LANDLORD_SQL, {"landlord_id": str(landlord.id)})

    return landlord, session
