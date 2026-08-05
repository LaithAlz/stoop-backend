"""SECURITY DEFINER identity lookup: landlord_id_for_auth_user(uuid) (#194)

Revision ID: 0019
Revises: 0017
Create Date: 2026-08-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.24
amendment block (2026-08-05). Read that block first.

NUMBERING NOTE (house convention -- see schema-v1.md's v1.16 amendment
"Numbering note, resolved" for the precedent this follows): the
orchestrator assigned this migration slot "0019" while a sibling lane was
concurrently assigned "0018", not yet present in this branch. down_revision
below chains from the actual current head in this worktree ("0017").
Whoever merges second reconciles the chain (down_revision edit only, no
DDL change) exactly as v1.11/v1.14/v1.16 record having been resolved
before them -- this is not a defect, it is the expected shape of two lanes
claiming adjacent slots concurrently.

WHY
---
From the deps.py flip-blocker fix's own safety review (see issue #194):
``app/deps.py::require_landlord`` opened a SECOND database connection
(``get_admin_session``) on every authenticated request, solely to resolve
``auth_user_id -> landlords.id`` before the RLS-scoping GUC could be set on
the caller's own request session (the ``landlords`` RLS policy is
``id``-keyed, so that id can never be found by a query that needs the GUC
set to it FIRST -- see ``app/deps.py``'s module docstring, "Identity-lookup
design history"). That correct-but-costly design created two couplings:

1. Fallback same-pool pressure (non-production only): with
   ``APP_DATABASE_URL`` unset, request and admin sessions share ONE 5+5
   pool, so every authenticated request held TWO connections
   simultaneously instead of one, roughly halving how many concurrent
   requests the pool could serve before the 30s pool_timeout cascade.
2. Admin-pool coupling to the emergency front door (post-flip):
   authenticated dashboard requests shared the admin pool with the
   UNAUTHENTICATED Twilio emergency-ingestion webhook
   (``routers/webhooks/twilio.py``, ``Depends(get_admin_session)``) --
   never-break rule #1 says the emergency line's resources must never
   queue behind dashboard reads.

WHAT THIS MIGRATION DOES
-------------------------
Adds exactly one new database object: a ``SECURITY DEFINER`` function,
``public.landlord_id_for_auth_user(p_auth_user_id uuid) RETURNS uuid``,
invoked on the CALLER'S OWN request session (``app/deps.py::
require_landlord``, same PR). Because ``SECURITY DEFINER`` runs with the
function OWNER's rights, the lookup bypasses RLS by construction --
exactly like the admin session it replaces -- but on the SAME connection,
so there is no second connection to open at all. Zero-downtime,
zero-migration-of-data: this migration only adds a function, nothing about
``landlords`` itself changes.

Follows migration 0004's ``SECURITY DEFINER`` precedent EXACTLY, per issue
#194's explicit instruction, including the two hard-won lessons from that
migration's own live Supabase dry-run ("the live dry-run rule"):

- **No dedicated owner role.** Migration 0004's original design created a
  narrowly-scoped, dedicated ``NOLOGIN`` owner role for its ``SECURITY
  DEFINER`` functions -- and that design proved impossible on live
  Supabase in three independent ways (``ALTER FUNCTION ... OWNER TO``
  itself failing, the ``pg_has_role(..., 'MEMBER')`` guard being unsound
  on Postgres 16+, and the self-grant fix terminating the connection
  outright -- see 0004's own "OWNERSHIP MODEL" section for the full
  writeup). This function is owned by whichever role runs this migration
  (``postgres`` on live Supabase, ``stoop`` locally) for the identical
  reason -- not a fresh decision, a repeat of an already-proven one.
- **Pinned ``search_path``.** ``SET search_path = public, pg_temp`` is
  the single most important line in this file: an UNPINNED
  ``search_path`` on a ``SECURITY DEFINER`` function is a
  privilege-escalation primitive -- a caller who can create an object
  (e.g. a same-named function or table) earlier in an unpinned search
  path can get the function to silently resolve to THEIR object instead
  of the real ``public.landlords``, executed with the DEFINER's full
  rights. Pinning to exactly ``public, pg_temp`` (never a caller
  -influenceable schema) closes that off categorically, matching migration
  0004's three functions verbatim.

  MUTATION-TESTING EVIDENCE (#194 safety review, reproduced by the
  orchestrator against a throwaway lane database; every probe ran inside a
  transaction -- with a ``SAVEPOINT`` around the mutation itself -- that
  was rolled back in full, leaving the database exactly as it was):
  re-creating this function WITHOUT the ``SET search_path`` clause, then
  calling it as ``app_role`` with ``SET LOCAL search_path = evil, public,
  pg_catalog`` (``evil`` being an attacker-created schema holding a
  backdoored ``uuid = uuid`` operator that always returns ``true``, naming
  ``pg_catalog`` explicitly to defeat its normal implicit-search-first
  behavior) turned a lookup for a WRONG/nonexistent ``auth_user_id`` into a
  real landlord's id being returned -- a genuine cross-tenant identity leak,
  live-reproduced, not a theoretical claim. Rolling back to the savepoint
  (restoring the real, pinned function) and repeating the IDENTICAL attack
  (same evil schema, same session search_path, same wrong argument)
  returned ``NULL``, exactly as intended: the function's own ``SET
  search_path`` clause overrides whatever the calling session set,
  regardless of what that session did first. This is precisely the
  vulnerability class the Postgres manual's "Writing SECURITY DEFINER
  Functions Safely" section warns about, confirmed against this exact
  function rather than assumed from the general case.

SECURITY -- why a single-argument identity-mapping function stays inside
the tenancy boundary (see schema-v1.md's v1.24 amendment for the full
five-point writeup; summarized here)
------------------------------------------------------------------------
The function returns AT MOST ONE row (``LIMIT 1``, and ``auth_user_id`` is
``UNIQUE`` besides), keyed purely on its one argument, and the ONLY caller
in this codebase (``app/deps.py::require_landlord``) always supplies
``user.user_id`` -- the ``sub`` claim of a signature-VERIFIED Supabase JWT,
never raw request input. This function is a pure identity-mapping utility,
not an authorization boundary: authorization is fully decided by JWT
verification BEFORE this function is ever invoked, exactly as it was
before this migration (the admin-session lookup it replaces had the
identical shape -- a plain ``auth_user_id`` bind parameter, never
client-controlled).

Deliberately NOT exception-wrapped, unlike migration 0004's trigger
functions: those must never block ``auth.users`` writes (015 spec gotcha),
so they swallow every error by design. This function instead backs an
ordinary authenticated API request -- a real failure here (connectivity,
an unexpected constraint) must surface as a request error, exactly like
the two-session admin lookup it replaces would have, never be silently
swallowed into a false ``account_deleted`` 403.

``STRICT`` (``RETURNS NULL ON NULL INPUT``): a ``NULL`` argument short
-circuits before the function body ever runs a query -- belt-and-braces on
top of the ordinary SQL three-valued-logic behavior (``auth_user_id =
NULL`` already matches zero rows), and guarantees a `NULL` input can never
be distinguished from a "no such row" input by timing or any other
side channel.

``STABLE`` (not ``VOLATILE``): the function only reads, never writes --
correct for the planner, and matches ``SELECT``'s already-committed-data
semantics within one statement.

EXECUTE GRANT
-------------
``PUBLIC`` revoked (defense-in-depth, matching migration 0004's three
functions), ``app_role`` granted explicitly -- required so the REQUEST
session (once ``APP_DATABASE_URL`` points at ``app_role``, per migration
0005 / schema-v1.md's v1.2 amendments) can call it at all. The function
owner (the migrating/admin role) always retains implicit ``EXECUTE``
regardless of any ``REVOKE``/``GRANT`` here, so the pre-flip admin-engine
fallback (``app/db/session.py``, ``APP_DATABASE_URL`` unset) keeps working
unchanged. ``app_role`` already exists by the time this migration runs
(created by migration 0005, which is always applied first in this chain),
so no existence guard is needed before granting to it.

DOWNGRADE
---------
Drops the function. No grant/role cleanup needed -- ``DROP FUNCTION``
removes every ACL entry on the object with it. ``app/deps.py``'s two
-session code path this migration's PR removes is a code-only revert (not
this migration's job); a downgrade of ONLY this migration, with the
application code left at its post-#194 state, would break
``require_landlord`` (the function it calls would no longer exist) --
exactly the same "migration and code must move together" contract every
other schema-shape migration in this repo already carries.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the SECURITY DEFINER identity-lookup function, owned by the
    migrating role, search_path pinned, PUBLIC EXECUTE revoked, app_role
    granted EXECUTE explicitly."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.landlord_id_for_auth_user(p_auth_user_id uuid)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        STABLE
        STRICT
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          v_landlord_id uuid;
        BEGIN
          SELECT id INTO v_landlord_id
            FROM public.landlords
           WHERE auth_user_id = p_auth_user_id
             AND deleted_at IS NULL
           LIMIT 1;

          RETURN v_landlord_id;
        END;
        $$;
        """
    )

    # Defense-in-depth (matching migration 0004): revoke the default PUBLIC
    # EXECUTE grant, then explicitly grant EXECUTE to app_role -- the one
    # role that actually needs to call this from the request path once
    # APP_DATABASE_URL is flipped (#22 / schema-v1.md v1.2 amendments).
    op.execute("REVOKE EXECUTE ON FUNCTION public.landlord_id_for_auth_user(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.landlord_id_for_auth_user(uuid) TO app_role")


def downgrade() -> None:
    """Drop the function. DROP FUNCTION removes its ACL entries with it --
    no separate REVOKE needed."""
    op.execute("DROP FUNCTION IF EXISTS public.landlord_id_for_auth_user(uuid)")
