"""auth.users -> landlords lifecycle sync (SECURITY DEFINER trigger, #15)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-05 00:00:00.000000

Canonical sources: docs/03-engineering/issue-specs/015-auth-user-lifecycle.md
(acceptance criteria, authoritative) and docs/03-engineering/schema-v1.md
(``landlords`` — no column changes needed here, see below).

WHAT THIS MIGRATION DOES
-------------------------
Adds a ``SECURITY DEFINER`` function set + triggers on ``auth.users`` so
sign-up, email change, and account deletion sync into ``landlords``
transactionally, without polling or webhooks:

1. ``AFTER INSERT`` on ``auth.users`` -> upsert a ``landlords`` row
   (``auth_user_id``, ``email``, ``full_name`` from
   ``raw_user_meta_data->>'full_name'``). Idempotent: ``ON CONFLICT
   (auth_user_id) DO UPDATE``.
2. ``AFTER UPDATE OF email`` on ``auth.users`` -> propagate the new email to
   the matching ``landlords`` row (UPDATE only — see "Email edge cases"
   below for why this is not an upsert).
3. ``AFTER DELETE`` on ``auth.users`` **and** ``AFTER UPDATE OF deleted_at``
   (``WHEN NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL``) -> soft
   -delete the matching ``landlords`` row (``deleted_at = now()``), never a
   hard delete. Both paths exist because Supabase/GoTrue sometimes hard-
   -deletes the ``auth.users`` row and sometimes soft-deletes it in place
   (015 spec hint) -- we must catch whichever one actually fires.

   NOTE -- this soft-delete is best-effort bookkeeping, not yet a security
   boundary: nothing in the request path currently filters on
   ``landlords.deleted_at`` (``GET /v1/me`` returns/upserts the row
   regardless -- see issue #135 part 1, not yet implemented), so a
   soft-deleted landlord's auth token would still work today. Closing that
   gap is #135 part 1's job, out of scope for #15.

``deleted_at`` ALREADY EXISTS -- no schema-v1.md change, no column added
--------------------------------------------------------------------------
The #15 acceptance criteria say "``deleted_at`` column migration included
here if not already present." It is already present: migration 0001
(``0001_create_landlords.py``) created ``landlords.deleted_at
timestamptz`` on day one, and schema-v1.md has carried it since (annotated
"soft delete (auth trigger #15)"). No doc edit and no ``ALTER TABLE`` are
needed or performed by this migration. ``downgrade()`` therefore does not
drop it either -- dropping a column this migration never added would be
wrong per the round-trip contract.

EMAIL EDGE CASES (consistent with #161)
-----------------------------------------
``landlords.email`` is ``NOT NULL``, but a Supabase phone-only signup can
reach this trigger with ``NEW.email`` being an empty string (GoTrue has no
``omitempty`` on that field) rather than an absent/NULL value -- the exact
same normalization problem PR #161 fixed at the JWT-verification boundary
(``app/integrations/supabase_auth.py::verify_jwt``). Both trigger functions
below normalize ``NEW.email`` via ``NULLIF(TRIM(NEW.email), '')`` and:

- **INSERT**: if the normalized email is NULL, the function returns without
  writing a ``landlords`` row at all. A phone-only auth user gets NO
  landlord row until they add a real email -- ``GET /v1/me`` already 403s
  ``email_required`` for a token with no usable email claim (#161), so
  nothing regresses; the lazy upsert there is exactly the safety net that
  provisions the row later once a real email exists.
- **UPDATE OF email**: if the normalized new email is NULL (e.g. an
  operator clears the field, or a webhook races), the function is a no-op
  -- it never overwrites a landlord's real stored email with blank. This
  mirrors the #161 regression test
  (``test_me_existing_landlord_empty_string_email_does_not_overwrite_stored_email``).

The rubric ``landlords.email NOT NULL`` constraint is never relaxed.

WHY "UPDATE OF email" IS UPDATE-ONLY, NOT AN UPSERT
------------------------------------------------------
The #15 acceptance criteria literally say "update matching landlord's
email" -- not "upsert." A phone-only auth user who later adds a real email
in Supabase (without ever calling ``GET /v1/me``) will not get a landlord
row from this trigger alone; that gap is intentionally left to the lazy
``GET /v1/me`` upsert (#11), which the issue explicitly calls "the safety
net" that "complements" this trigger. Scope discipline: adding upsert
semantics to the email-update path was not asked for and is not added here.

OWNERSHIP MODEL -- redesigned after a live Supabase dry-run
------------------------------------------------------------------------
An earlier version of this migration created a dedicated ``NOLOGIN``
``landlord_sync_role``, transferred function ownership to it, and revoked
``PUBLIC`` ``EXECUTE`` -- on the theory that a ``SECURITY DEFINER``
function should be owned by the narrowest role possible rather than the
migration-running role. A live Supabase dry-run proved that design
impossible on Supabase's actual platform, in three independent ways
(reproduced by the orchestrator; every probe ran inside a transaction
that was rolled back -- the live database was left clean at revision
0003, and no probe role/object was left behind):

1. Even after fixing the CREATE-TRIGGER-before-ownership-transfer
   statement ordering (correct in isolation, but never actually reached),
   ``ALTER FUNCTION ... OWNER TO landlord_sync_role`` itself failed on
   live Supabase with ``must be able to SET ROLE "landlord_sync_role"``.
2. The guard this migration used to decide whether the migrating role
   already had the required membership -- ``pg_has_role(current_user,
   ..., 'MEMBER')`` -- is unsound on Postgres 16+: a ``CREATEROLE``
   -privileged creator holds *implicit ADMIN OPTION* on any role it just
   created, which makes ``pg_has_role(..., 'MEMBER')`` return ``TRUE``
   before any actual membership ``GRANT`` ever ran. The self-grant guard
   was therefore always skipped, silently, in exactly the environment it
   needed to fire.
3. Attempting to fix #2 directly -- ``GRANT landlord_sync_role TO
   CURRENT_USER`` issued as ``postgres`` -- is not just denied, it
   **terminates the connection mid-operation** on live Supabase. This is
   platform-level role protection, reproduced independently over both the
   Supavisor transaction pooler (port 6543) and the session pooler (port
   5432).

DECISION: ``landlord_sync_role`` is removed entirely. All three
``SECURITY DEFINER`` functions remain owned by whichever role runs this
migration (``postgres`` on live Supabase, ``stoop`` locally) -- this is
*Supabase's own documented pattern* for exactly this use case (their
canonical ``handle_new_user()`` example, referenced in the 015 spec
hints, is owned by the migrating role, not a separately-provisioned one).

TRADEOFF -- what this costs, and why it's acceptable for #15
------------------------------------------------------------------------
Because the functions are no longer owned by a narrowly-scoped role, each
one now executes (as ``SECURITY DEFINER``) with the **migrating role's
full rights** -- on live Supabase, an admin-tier surface, not just
``SELECT/INSERT/UPDATE`` on ``landlords``. The **only** containment left
is the function body itself:

- every statement is fixed/static -- no dynamic SQL, no ``EXECUTE
  format(...)``, nothing built from untrusted input, ever;
- ``SET search_path = public, pg_temp`` stays pinned on all three
  functions -- the classic ``SECURITY DEFINER`` search-path-injection
  guard is independent of who owns the function and still fully applies;
- ``REVOKE EXECUTE ... FROM PUBLIC`` still runs, still *after* ``CREATE
  TRIGGER`` (the one piece of the earlier statement-ordering fix that
  remains in this file, for consistency/lowest-risk-by-default even
  though it's no longer load-bearing here: since the function's owner
  never changes, the creating role always retains implicit ``EXECUTE`` on
  it regardless of when ``PUBLIC`` is revoked).

Issue #15's acceptance criterion ("function owned by a role that can
write ``landlords`` but is not the app's request role") is **not
currently satisfied, and is deliberately deferred to #22** rather than
claimed here: today, the API itself connects to Postgres as the same
role that owns these functions (``postgres`` on live Supabase; there is
no separate ``app_role``/``authenticated`` Postgres login role yet --
that request-scoped role is created by #22's RLS work, not by anything
that exists today). Calling the criterion "satisfied" before that role
exists would be describing a future state as a present fact. This is a
platform-forced tradeoff, not a design choice: sections 1-3 above are why
the originally-intended dedicated-role design (which *would* have
satisfied this criterion today) is impossible on Supabase as currently
provisioned.

Least-privilege function ownership (a dedicated role narrower than the
full migrating-role admin surface, satisfying #15's criterion for real)
is **deferred to #22**, where the complete role model
(``app_role``/``authenticated``, RLS policies, and whatever grants a
``SECURITY DEFINER`` trigger function can safely take on) gets designed
against these now-known platform constraints, instead of guessed at here
and broken again on the next live dry-run.

EXCEPTION SAFETY -- never block sign-up/update/delete
----------------------------------------------------------
Per the 015 spec gotcha ("a trigger failure on auth.users insert can block
sign-up entirely"), every function body is wrapped in
``EXCEPTION WHEN OTHERS THEN ... RETURN NEW/OLD`` -- any error is swallowed
(logged via ``RAISE WARNING`` with only ``auth_user_id`` and ``SQLERRM``,
never email/name) and the triggering statement on ``auth.users`` always
succeeds. The lazy ``GET /v1/me`` upsert is the backstop for anything
silently missed here.

LOCAL/CI TEST SHIM -- ``auth`` schema + minimal ``auth.users`` table
------------------------------------------------------------------------
Local docker Postgres (and CI) has no ``auth`` schema and no Supabase
GoTrue-managed ``auth.users`` table; live Supabase always has both already.
``upgrade()`` therefore creates ``auth`` + a minimal ``auth.users`` shim
**only when the ``auth`` schema does not already exist** (guarded by
``pg_namespace`` lookup) -- so this is a strict no-op against a real
Supabase database. The shim reproduces only the column subset this
migration's triggers touch: ``id uuid PRIMARY KEY``, ``email text``,
``raw_user_meta_data jsonb NOT NULL DEFAULT '{}'``, ``deleted_at
timestamptz``. Both the shim schema and its table carry a ``COMMENT``
beginning with ``stoop-local-shim`` -- ``downgrade()`` reads that comment
back and drops the shim **only if it is present**, so a downgrade run
against live Supabase (where the marker comment is absent because the
schema pre-existed) never touches the real ``auth`` schema.

The function/trigger DDL itself (the actual deliverable) is created
IDENTICALLY in both environments -- the shim only exists so the same
``CREATE TRIGGER ... ON auth.users`` statements have somewhere valid to
attach locally.

DOWNGRADE
---------
Reverses upgrade() in dependency order: triggers, then functions, then
(guarded by the marker comment) the shim ``auth`` schema.
``landlords.deleted_at`` is left alone (this migration did not add it).
``DROP FUNCTION`` needs no special role handling now: each function is
owned by the same migrating role that is running this ``downgrade()``
(or a superuser), so ordinary ownership is always sufficient.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the local auth.users shim (guarded) and the SECURITY DEFINER
    functions + auth.users triggers, owned by the migrating role."""

    # ── local/CI test shim: auth schema + minimal auth.users table ──────────
    # Guarded: only runs when `auth` does not already exist (never true on
    # live Supabase). Marked with a COMMENT so downgrade() can tell it apart
    # from a real Supabase `auth` schema.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth') THEN
            CREATE SCHEMA auth;
            COMMENT ON SCHEMA auth IS
              'stoop-local-shim (migration 0004): minimal local substitute '
              'for the Supabase auth schema. Safe to drop only when this '
              'exact marker is present (checked by migration 0004 '
              'downgrade()).';

            CREATE TABLE auth.users (
              id                  uuid PRIMARY KEY,
              email               text,
              raw_user_meta_data  jsonb NOT NULL DEFAULT '{}'::jsonb,
              deleted_at          timestamptz
            );
            COMMENT ON TABLE auth.users IS
              'stoop-local-shim (migration 0004): minimal substitute for '
              'Supabase-managed auth.users (real schema owned by GoTrue) '
              '-- id/email/raw_user_meta_data/deleted_at subset only, '
              'sufficient for the #15 trigger tests.';
          END IF;
        END $$;
        """
    )

    # ── AFTER INSERT: upsert landlords row; skip if email is blank ─────────
    # Owned by the migrating role (see docstring "OWNERSHIP MODEL") --
    # SECURITY DEFINER means it always runs with that role's rights,
    # regardless of who/what actually fires the INSERT on auth.users
    # (GoTrue's own service role in production).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.handle_auth_user_created()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          v_email text;
        BEGIN
          v_email := NULLIF(TRIM(NEW.email), '');

          IF v_email IS NULL THEN
            -- Phone-only signup (or a present-but-blank email row):
            -- landlords.email is NOT NULL, so provisioning is deliberately
            -- skipped here (see migration docstring "EMAIL EDGE CASES").
            -- GET /v1/me's lazy upsert (#11) provisions the row once a
            -- real, non-empty email claim exists.
            RETURN NEW;
          END IF;

          INSERT INTO public.landlords (auth_user_id, email, full_name)
          VALUES (NEW.id, v_email, NEW.raw_user_meta_data ->> 'full_name')
          ON CONFLICT (auth_user_id) DO UPDATE
            SET email      = EXCLUDED.email,
                full_name  = COALESCE(EXCLUDED.full_name, public.landlords.full_name),
                updated_at = now();

          RETURN NEW;
        EXCEPTION
          WHEN OTHERS THEN
            -- Never block sign-up (015 spec gotcha). No PII in the
            -- warning: auth_user_id only, never email/full_name.
            RAISE WARNING 'handle_auth_user_created failed for auth_user_id=%: %',
              NEW.id, SQLERRM;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_created
          AFTER INSERT ON auth.users
          FOR EACH ROW EXECUTE FUNCTION public.handle_auth_user_created()
        """
    )
    # Defense-in-depth (safety review): a trigger function can't usefully be
    # called directly via SQL anyway, but revoke the default PUBLIC EXECUTE
    # grant so it isn't even listed as callable.
    op.execute("REVOKE EXECUTE ON FUNCTION public.handle_auth_user_created() FROM PUBLIC")

    # ── AFTER UPDATE OF email: propagate; never overwrite with blank ───────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.handle_auth_user_email_updated()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          v_email text;
        BEGIN
          v_email := NULLIF(TRIM(NEW.email), '');

          IF v_email IS NULL THEN
            -- Same '' / whitespace normalization as #161: never clobber a
            -- stored landlord email with a blank value.
            RETURN NEW;
          END IF;

          -- UPDATE-only, deliberately not an upsert -- see migration
          -- docstring "WHY UPDATE OF email IS UPDATE-ONLY".
          UPDATE public.landlords
             SET email      = v_email,
                 updated_at = now()
           WHERE auth_user_id = NEW.id;

          RETURN NEW;
        EXCEPTION
          WHEN OTHERS THEN
            RAISE WARNING 'handle_auth_user_email_updated failed for auth_user_id=%: %',
              NEW.id, SQLERRM;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_email_updated ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_email_updated
          AFTER UPDATE OF email ON auth.users
          FOR EACH ROW
          WHEN (NEW.email IS DISTINCT FROM OLD.email)
          EXECUTE FUNCTION public.handle_auth_user_email_updated()
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION public.handle_auth_user_email_updated() FROM PUBLIC")

    # ── AFTER DELETE / AFTER UPDATE OF deleted_at: soft-delete, never hard ─
    # Two triggers share this one function.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.handle_auth_user_soft_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
          v_id uuid;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            v_id := OLD.id;
          ELSE
            v_id := NEW.id;
          END IF;

          -- Never a hard delete. COALESCE preserves the first soft-delete
          -- timestamp if this fires more than once for the same row.
          UPDATE public.landlords
             SET deleted_at = COALESCE(deleted_at, now()),
                 updated_at = now()
           WHERE auth_user_id = v_id;

          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        EXCEPTION
          WHEN OTHERS THEN
            RAISE WARNING 'handle_auth_user_soft_delete failed for auth_user_id=%: %',
              v_id, SQLERRM;
            IF TG_OP = 'DELETE' THEN
              RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_deleted
          AFTER DELETE ON auth.users
          FOR EACH ROW EXECUTE FUNCTION public.handle_auth_user_soft_delete()
        """
    )
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted_at_updated ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_deleted_at_updated
          AFTER UPDATE OF deleted_at ON auth.users
          FOR EACH ROW
          WHEN (NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL)
          EXECUTE FUNCTION public.handle_auth_user_soft_delete()
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION public.handle_auth_user_soft_delete() FROM PUBLIC")


def downgrade() -> None:
    """Exactly reverse upgrade(): drop triggers, functions, and the shim
    auth schema (guarded by its marker comment). landlords.deleted_at is
    left untouched -- this migration never added it. No role handling: the
    functions were never owned by anything but the migrating role.
    """

    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted_at_updated ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_email_updated ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users")

    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_soft_delete()")
    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_email_updated()")
    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_created()")

    # Guarded shim teardown: only drop `auth` if it carries our marker
    # comment -- never touch a real Supabase `auth` schema.
    op.execute(
        """
        DO $$
        DECLARE
          v_comment text;
        BEGIN
          SELECT obj_description(oid, 'pg_namespace') INTO v_comment
            FROM pg_namespace WHERE nspname = 'auth';
          IF v_comment IS NOT NULL
             AND v_comment LIKE 'stoop-local-shim (migration 0004)%' THEN
            DROP SCHEMA auth CASCADE;
          END IF;
        END $$;
        """
    )
