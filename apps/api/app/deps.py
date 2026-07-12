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

Two-session rationale for ``require_landlord`` (found during #54/#55/#57's
spec review; fixed here since it blocks every endpoint those issues add)
------------------------------------------------------------------------
``landlords`` is RLS-enabled and (schema-v1.md's v1.2 amendments) is
**id-keyed, not landlord_id-keyed**: its one policy's ``USING`` clause is
``id = current_setting('app.current_landlord_id', true)::uuid``. That policy
covers SELECT too (a single ``FOR ALL`` policy, no separate read rule). This
is a genuine chicken-and-egg problem for the ORIGINAL single-session design
(look up the row on the request-scoped session, THEN set the GUC on that
same session): once ``APP_DATABASE_URL`` actually points at ``app_role``
(the documented production role-separation flip in ``app/db/session.py``),
the very FIRST query on the request-path session — ``SELECT id FROM
landlords WHERE auth_user_id = ...`` — runs BEFORE the GUC is ever set. An
unset GUC reads back as SQL ``NULL`` (``current_setting(..., true)``'s
``missing_ok`` behavior), and ``id = NULL`` is never true for any row —
so the lookup returns ZERO rows for every caller, always, forever, even
for a real, live, correctly-provisioned landlord. The GUC would have to
already equal the row's ``id`` before that row could ever be found by this
query, which is exactly backwards — the same structural bug ``routers/
me.py``'s module docstring already documents for the ``landlords`` INSERT
path ("a freshly ``gen_random_uuid()``'d id can never equal a GUC value
that would have to be set BEFORE that id exists"), just on the SELECT side
of the same table instead of the INSERT side. Locally this was invisible:
``APP_DATABASE_URL`` is unset in dev/CI, so ``get_session`` falls back to
the admin engine, and Postgres superusers always bypass RLS regardless of
the GUC (``app/db/session.py``'s module docstring) — every existing test
that exercises ``require_landlord`` end-to-end was, without realizing it,
running the lookup with RLS effectively off. The fix below is proven
under REAL enforcement (``SET LOCAL ROLE app_role``, the migration-0005
test convention) in ``tests/test_require_landlord.py``.

The fix: TWO sessions, same pattern ``GET /v1/me`` already established for
this exact class of problem — resolve ``landlords.id`` on a short-lived,
RLS-UNSCOPED **admin** session (``get_admin_session``, scoped narrowly to
just this one lookup via the same ``asynccontextmanager(get_admin_session)``
idiom already used by ``app/agent/nodes/identify_property.py``/
``load_context.py`` — NOT as a ``Depends(get_admin_session)`` parameter,
which would hold an extra admin-pool connection open for the entire
request lifetime on literally every authenticated endpoint), then set the
GUC on the caller's REAL request-path ``session`` (the one ``get_session``
yields and every downstream query in the handler actually uses) before
returning it. The identity lookup never needs RLS in the first place (it
is filtered by ``auth_user_id``, not landlord-scoped data), and the GUC
still ends up set on exactly the session whose transaction commits/rolls
back at request teardown — nothing about the GUC's lifetime or scoping
semantics (the ``is_local``/``SET LOCAL`` notes below) changes.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_session, get_session
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
    soft-deleted rows) on a short-lived ADMIN (RLS-unscoped) session — see
    the module docstring's "Two-session rationale" for why this lookup
    structurally cannot run under RLS at all, even correctly, no matter
    what GUC value it tries — then sets the ``app.current_landlord_id``
    Postgres session variable that migration 0005's RLS policies key off
    on the REAL request-path ``session`` (the one this function returns,
    and the only one any handler ever touches) — every subsequent query on
    that session is scoped to this landlord automatically, fail-closed if
    this were somehow skipped (an unset GUC reads back as ``NULL``, which
    matches zero rows under RLS).

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
    async with asynccontextmanager(get_admin_session)() as admin_session:
        result = await admin_session.execute(
            _LANDLORD_LOOKUP_SQL, {"auth_user_id": str(user.user_id)}
        )
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
