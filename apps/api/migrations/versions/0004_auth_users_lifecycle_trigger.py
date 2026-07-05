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

ISOLATION -- dedicated NOLOGIN role owns the functions
---------------------------------------------------------
``landlord_sync_role`` is a ``NOLOGIN`` role, created if absent, granted
only ``SELECT, INSERT, UPDATE`` on ``landlords`` (no other table, no
``DELETE`` -- ``SELECT`` is required too, since ``ON CONFLICT DO UPDATE``
and the WHERE-filtered ``UPDATE``s reference existing column values).
All three trigger functions are ``SECURITY DEFINER`` with ``SET
search_path = public, pg_temp`` (the standard Postgres privilege-escalation
guard for ``SECURITY DEFINER`` -- per the "Common gotchas" section of
docs/03-engineering/issue-specs/015-auth-user-lifecycle.md: "SECURITY
DEFINER + explicit set search_path -- without the search_path pin this is
a privilege-escalation foot-gun"), and are then
``ALTER FUNCTION ... OWNER TO landlord_sync_role``. Each function also has
``EXECUTE`` revoked from ``PUBLIC`` (defense-in-depth -- a trigger function
can't usefully be invoked directly via SQL anyway since Postgres rejects a
direct call with "trigger functions can only be called as triggers," and
``CREATE TRIGGER``/the trigger manager itself never needs the invoking
session to hold ``EXECUTE`` on the function; only removes a no-op grant).
A ``SECURITY DEFINER``
function always executes with its **owner's** privileges, never the
caller's -- so on live Supabase, GoTrue's own connection (or whatever role
the direct DB connection uses) never itself needs write access to
``landlords``; it only needs the (ordinary, already-required) privilege to
insert/update/delete ``auth.users`` rows, which triggers this function via
the owner's rights. This also means the functions are deliberately NOT
owned by the migration-running role (``postgres`` on Supabase, ``stoop``
locally) -- owning a ``SECURITY DEFINER`` function by a superuser-ish role
is the classic privilege-escalation footgun the 015 spec's gotchas call
out; a narrowly-scoped, login-disabled role is the safer owner.

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
(guarded) the ``landlord_sync_role`` grant + role, then (guarded by the
marker comment) the shim ``auth`` schema. ``landlords.deleted_at`` is left
alone (this migration did not add it).

``landlord_sync_role`` itself is only actually dropped if ``pg_shdepend``
shows no remaining cluster-wide dependents (see the long comment in
``downgrade()``) -- because roles are cluster-global, a sibling database
that also applied 0004 would otherwise make an unconditional ``DROP ROLE``
fail and roll back the whole downgrade. When that guard trips, the
NOLOGIN, now-privilege-free role is left in place; that's safe, not a
security gap.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# NOTE: the literal marker text below ("stoop-local-shim (migration 0004)")
# is duplicated verbatim in the upgrade()/downgrade() SQL strings rather
# than interpolated via an f-string/`.format()` — ruff (S608) flags
# string-built SQL as a possible injection vector even when, as here, the
# interpolated value is a fixed module-level constant, never external
# input. Keeping the raw literal inline avoids the false positive while
# staying byte-identical across both call sites (grep for it if it ever
# needs to change).


def upgrade() -> None:
    """Create the local auth.users shim (guarded), the sync role, the
    SECURITY DEFINER functions, and the auth.users triggers."""

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

    # ── dedicated NOLOGIN role: owns the trigger functions, can write only
    # landlords.INSERT/UPDATE. Guarded (CREATE ROLE has no IF NOT EXISTS). ──
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landlord_sync_role') THEN
            CREATE ROLE landlord_sync_role NOLOGIN;
          END IF;
        END $$;
        """
    )
    # SELECT is required in addition to INSERT/UPDATE: the ON CONFLICT DO
    # UPDATE clause reads the existing `landlords.full_name` (for the
    # COALESCE) and both trigger functions filter their UPDATE with a WHERE
    # auth_user_id = ... clause -- Postgres requires SELECT privilege on any
    # column referenced that way, not just the ones being written.
    op.execute("GRANT SELECT, INSERT, UPDATE ON landlords TO landlord_sync_role")

    # ── AFTER INSERT: upsert landlords row; skip if email is blank ─────────
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
    op.execute("ALTER FUNCTION public.handle_auth_user_created() OWNER TO landlord_sync_role")
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
    op.execute("ALTER FUNCTION public.handle_auth_user_email_updated() OWNER TO landlord_sync_role")
    op.execute("REVOKE EXECUTE ON FUNCTION public.handle_auth_user_email_updated() FROM PUBLIC")

    # ── AFTER DELETE / AFTER UPDATE OF deleted_at: soft-delete, never hard ─
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
    op.execute("ALTER FUNCTION public.handle_auth_user_soft_delete() OWNER TO landlord_sync_role")
    op.execute("REVOKE EXECUTE ON FUNCTION public.handle_auth_user_soft_delete() FROM PUBLIC")

    # ── triggers on auth.users ───────────────────────────────────────────────
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_created
          AFTER INSERT ON auth.users
          FOR EACH ROW EXECUTE FUNCTION public.handle_auth_user_created()
        """
    )

    op.execute("DROP TRIGGER IF EXISTS on_auth_user_email_updated ON auth.users")
    op.execute(
        """
        CREATE TRIGGER on_auth_user_email_updated
          AFTER UPDATE OF email ON auth.users
          FOR EACH ROW EXECUTE FUNCTION public.handle_auth_user_email_updated()
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


def downgrade() -> None:
    """Exactly reverse upgrade(): drop triggers, functions, the sync role
    (guarded), and the shim auth schema (guarded by its marker comment).
    landlords.deleted_at is left untouched -- this migration never added it.
    """

    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted_at_updated ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_deleted ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_email_updated ON auth.users")
    op.execute("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users")

    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_soft_delete()")
    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_email_updated()")
    op.execute("DROP FUNCTION IF EXISTS public.handle_auth_user_created()")

    # Guarded: revoke + drop only if the role exists (safe on a repeated
    # downgrade). Privileges granted to a role must be revoked before it can
    # be dropped.
    #
    # ``landlord_sync_role`` is a CLUSTER-GLOBAL object (roles aren't
    # per-database) -- if a sibling database in the same Postgres cluster
    # also has migration 0004 applied, that other database's grants/function
    # ownership still reference this role after our local REVOKE above
    # finishes, and an unconditional DROP ROLE fails ("role ... cannot be
    # dropped because some objects depend on it"), rolling back this entire
    # downgrade (reproduced via safety review: pg_shdepend showed 4
    # dependent objects in another database). ``pg_shdepend`` records every
    # shared (cluster-wide) dependency on a role across ALL databases, so
    # checking it -- not just this database's local state -- after our own
    # REVOKE is the only reliable "is anything else still using this role"
    # test. If something else still is, we deliberately leave the harmless
    # NOLOGIN, no-privilege role in place rather than fail the downgrade;
    # it grants nothing and can't log in, so leaving it behind is a no-op
    # from a security standpoint.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'landlord_sync_role') THEN
            REVOKE SELECT, INSERT, UPDATE ON landlords FROM landlord_sync_role;
            IF NOT EXISTS (
              SELECT 1 FROM pg_shdepend
              WHERE refobjid = 'landlord_sync_role'::regrole
            ) THEN
              DROP ROLE landlord_sync_role;
            END IF;
          END IF;
        END $$;
        """
    )

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
