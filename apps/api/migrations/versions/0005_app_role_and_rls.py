"""app_role + Row-Level Security on every schema-v1 table (#22)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md (the v1.2
amendments block, dated 2026-07-05, describes exactly what this migration
does). Issue #22's acceptance criteria list 9 tables; this migration covers
EVERY table in schema-v1.md (13, all but ``alembic_version``) per
apps/api/CLAUDE.md ("RLS isolation tests (#23) must cover every table in
schema-v1.md") — including ``vendors``, ``drafts``, ``landlords`` (the
issue's 9 plus these 3), and ``message_status_events`` (added by migration
0003, after the issue was written, but unambiguously a schema-v1.md table
with a ``message_id`` FK to scope by, so it gets the same treatment as
``message_cases``). Leaving any real table out of RLS coverage would be an
actual isolation gap, not a scope-discipline win.

WHY THIS LOOKS DIFFERENT FROM A "NORMAL" SUPABASE PROJECT
------------------------------------------------------------------------
On a fresh Supabase project the migrating role is ``postgres`` — a
NON-superuser role (Supabase reserves real superuser for its own
infrastructure). Two hard platform constraints, live-verified during the
#15 (migration 0004) dry-run and reconfirmed for this migration, shape
everything below:

1. ``GRANT <role> TO CURRENT_USER`` (or anything else that modifies
   ``postgres``'s own role memberships) **terminates the connection**
   outright on live Supabase. Never do it, anywhere, ever.
2. Guards based on ``pg_has_role(current_user, ..., 'MEMBER')`` are
   UNSOUND on Postgres 16+: a ``CREATEROLE``-privileged creator holds
   *implicit ADMIN OPTION* on any role it just created, so that check
   returns ``TRUE`` before any real membership ``GRANT`` ever ran. Use
   plain catalog lookups (``pg_roles``, ``pg_shdepend``) instead — never
   ``pg_has_role(...)`` as an idempotency guard.

Locally, ``docker-compose``'s Postgres runs as a bootstrap SUPERUSER
(``stoop``), which trivially satisfies everything a plain non-superuser
role could only reach via careful grants. Every statement below is written
to work correctly under BOTH regimes: plain ``CREATE ROLE`` and plain
``GRANT <privilege> TO <role>`` (never ``GRANT <role> TO ...``) are fine on
both; nothing here needs superuser, and nothing here self-grants.

LIVE ROLE FACTS (verified against live Supabase, #22 safety review)
------------------------------------------------------------------------
``SELECT rolname, rolsuper, rolbypassrls FROM pg_roles`` on the live
project:

- ``postgres`` — NOT a superuser, but ``rolbypassrls = TRUE``.
- ``service_role`` — NOT a superuser, but ``rolbypassrls = TRUE``.
- ``authenticated``, ``anon``, ``authenticator`` — ``rolbypassrls = FALSE``.

This is why ``FORCE ROW LEVEL SECURITY`` is deliberately NOT used anywhere
below — see step 3.

WHAT THIS MIGRATION DOES
-------------------------
1. ``CREATE ROLE app_role NOLOGIN`` — idempotent via a ``pg_roles`` catalog
   lookup (never ``pg_has_role``). **No password, ever, in any
   migration.** See "PRODUCTION OPERATOR STEP" below for how one gets set.
   Immediately followed by a defensive, unconditional
   ``ALTER ROLE app_role NOLOGIN`` (#22 safety review item 10): if a role
   named ``app_role`` already existed with LOGIN enabled (e.g. a stale
   role left over from an interrupted local/CI test run that grants a
   temporary password — see ``tests/test_rls_isolation.py``'s module
   docstring for exactly that failure mode, now eliminated but guarded
   against here too), the existence-guarded ``CREATE ROLE`` above is a
   no-op and would otherwise silently leave that stale LOGIN-enabled state
   in place across a re-migration. Unconditional ``ALTER ROLE ... NOLOGIN``
   is always safe to run (no-op if already ``NOLOGIN``) and needs no
   guard of its own.

2. Grants app_role:
   - ``USAGE`` on schema ``public``.
   - ``SELECT, INSERT, UPDATE, DELETE`` on the 10 ordinary tables:
     ``landlords, properties, vendors, tenants, cases, message_cases,
     drafts, trust_metrics, notifications, push_tokens``.
   - ``SELECT, INSERT`` ONLY on the 3 append-only tables: ``messages,
     audit_log, message_status_events`` — followed by an explicit
     ``REVOKE UPDATE, DELETE`` on the same three for self-documentation
     (never-break rule #2). Migrations 0002/0003 each carried a comment
     saying this REVOKE was "DEFERRED to #22" — this is that closure;
     ``test_append_only_revoke_gate_documented`` (0002) and its 0003
     analogue stay green (they only assert the deferred-gate comment is
     still present, not that the REVOKE has landed — that's covered by
     new catalog assertions in ``tests/test_migrations_0005.py``).
   - ``USAGE`` on the two identity-column sequences backing ``audit_log.id``
     and ``message_status_events.id`` (looked up via
     ``pg_get_serial_sequence`` rather than hardcoding
     ``<table>_id_seq``, which is the default name but not a documented
     guarantee).

3. Row-Level Security — ``ENABLE ROW LEVEL SECURITY`` ONLY (no ``FORCE``)
   on all 13 tables, one ``FOR ALL TO app_role`` policy per table:
   - Direct ``landlord_id`` match (10 tables: properties, vendors,
     tenants, cases, messages, drafts, trust_metrics, audit_log,
     notifications, push_tokens):
     ``USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)``
     with an identical ``WITH CHECK``.
   - ``landlords`` (keyed on ``id``, it has no ``landlord_id`` column):
     same shape, keyed on ``id`` instead.
   - ``message_cases`` (no ``landlord_id``): ``EXISTS`` join through
     ``cases`` on ``case_id``.
   - ``message_status_events`` (no ``landlord_id``, v1.1): ``EXISTS`` join
     through ``messages`` on ``message_id``.

   ``current_setting(..., true)`` — the ``true`` is ``missing_ok``: an
   unset GUC reads back as SQL ``NULL`` (never an error), and
   ``landlord_id = NULL`` (or ``id = NULL``) is never true in SQL
   three-valued logic — so a session that never calls
   ``set_config('app.current_landlord_id', ...)`` sees zero rows and can
   write zero rows. Fail closed by construction, not by convention.

   WHY NO ``FORCE`` (#22 safety review item 2 — changed after the first
   draft of this migration, which DID use ``FORCE`` on every table):
   ``FORCE ROW LEVEL SECURITY`` only changes ONE thing — whether the
   TABLE OWNER is also subject to RLS (owners normally bypass their own
   tables' policies entirely). It does nothing for ``app_role``, which was
   never the owner of anything and is therefore ALWAYS fully subject to
   RLS the moment ``ENABLE`` runs, ``FORCE`` or not — every behavioral
   test in ``tests/test_rls_isolation.py`` proves ``app_role`` isolation
   works identically either way. What ``FORCE`` WOULD have done is bind
   the OWNER/ADMIN role (``postgres`` on live Supabase, ``stoop`` locally)
   to RLS too — and the admin/owner path is a DELIBERATE, INTENTIONAL
   service path, not an accident to close off:
     - ``GET /v1/me``'s provisioning upsert (``routers/me.py``,
       ``get_admin_session``) creates the very ``landlords`` row
       ``require_landlord`` needs to set the GUC from — there is no GUC
       to set for a row that doesn't exist yet, so this path MUST run
       unscoped.
     - Migration 0004's ``auth.users`` lifecycle trigger runs
       ``SECURITY DEFINER`` as the owner, entirely outside any request's
       GUC context.
     - Webhook ingestion (#40, forward note — see also
       ``app/db/session.py``'s module docstring) will need the same
       unscoped admin path: an RLS-scoped session could silently drop an
       inbound emergency message if landlord resolution fails, which is
       exactly the catastrophic direction never-break rule #1 (the
       emergency line is never gated) forbids.
   Per the "LIVE ROLE FACTS" above, ``postgres``/``service_role`` both
   already hold ``rolbypassrls = TRUE`` on live Supabase today, so `FORCE`
   would have been a complete no-op there regardless (``BYPASSRLS`` always
   wins over ``FORCE``, for any role, superuser or not). But relying on
   that today-true fact would have been fragile: in ANY environment where
   the owner/admin role did NOT hold ``BYPASSRLS`` (a plausible future
   Supabase change, or a differently-provisioned environment), ``FORCE``
   would have bound the 0004 trigger and pre-flip ``/v1/me`` to RLS with
   no GUC in scope — the trigger's own ``EXCEPTION WHEN OTHERS`` handler
   would SWALLOW the resulting RLS error and return successfully anyway
   (by design, per its own "never block sign-up" contract), silently
   losing the ``landlords`` row with no error surfaced ANYWHERE — a
   silent sign-up lockout. Dropping ``FORCE`` entirely removes this whole
   failure class categorically, rather than depending on a role attribute
   that this migration does not control and did not set.

4. Belt-and-braces against the Supabase Data API bypass channel, two
   layers, both guarded by a ``pg_roles`` existence check per role name
   (``anon``/``authenticated`` exist only on live Supabase — PostgREST's
   roles — silent no-op locally):
   - ``REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated``
     — closes access to every table that exists RIGHT NOW.
   - ``ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM
     anon, authenticated`` (#22 safety review item 4) — closes access to
     every table THIS MIGRATING ROLE creates in ``public`` in the FUTURE
     (i.e. every later migration, since they all run as the same role).
     ``ALTER DEFAULT PRIVILEGES`` with no explicit ``FOR ROLE`` clause
     scopes to "objects later created BY THE ROLE RUNNING THIS STATEMENT"
     — exactly what we want, since every migration in this repo runs as
     that same role. STANDING NOTE (also recorded in schema-v1.md's v1.2
     block): this does NOT cover a table created in a DIFFERENT schema, or
     created by a DIFFERENT role (e.g. via the Supabase Studio UI using
     its own service credentials) — any new schema or differently-owned
     table needs the same explicit treatment, it is not automatically
     inherited from this migration.
   The Supabase dashboard's Data API toggle should ALSO be disabled for
   this project — a human step (Fly/Supabase account access), not
   something a migration can reach. Neither REVOKE step is reversed in
   ``downgrade()`` — see "DOWNGRADE" below.

5. LangGraph checkpoint tables (``AsyncPostgresSaver.setup()``, #24) do not
   exist yet. When they land, they belong in a dedicated schema reached
   ONLY via the admin engine (``app/db/session.py``'s ``engine``), never
   ``app_role`` — that isolation work ships with the graph work itself,
   not here. Documented, not implemented, by design.

6. Deliberately excluded from RLS: ``alembic_version`` (migrations-only,
   no ``landlord_id``, never queried by request-path code) and the local
   -only ``auth.users`` shim (migration 0004) — a real Supabase project's
   actual ``auth.users`` is in a schema GoTrue owns and secures itself;
   the local shim exists purely so migration 0004's triggers have
   somewhere to attach and is out of scope for this migration.

PRODUCTION OPERATOR STEP (never done here, never in any migration)
------------------------------------------------------------------------
``app_role`` is created ``NOLOGIN`` — nothing can authenticate as it until
a human, once, directly against the target database, runs:

    ALTER ROLE app_role LOGIN PASSWORD '<a freshly generated secret>';

...and then sets the ``APP_DATABASE_URL`` Fly secret (same Supavisor pooler
host as ``DATABASE_URL``, different user/password — see
``app/db/session.py`` module docstring for the full request-engine design
this enables). Do this BEFORE any tenant data exists in the target
database — flipping role separation on after real landlord data is
already flowing through the admin-engine-as-request-engine fallback is a
migration project of its own, not a config flip. ``app/config.py``'s
``Settings`` refuses to boot at all in ``ENVIRONMENT=production`` without
``APP_DATABASE_URL`` set (#22 safety review item 3), so this step cannot be
silently skipped in production.

DOWNGRADE
---------
Reverses upgrade() in dependency-safe order: drop all 13 policies, then
``DISABLE ROW LEVEL SECURITY`` on all 13 tables, then (guarded by a
``pg_roles`` existence check) revoke every grant ``app_role`` holds —
``REVOKE ALL ON ALL TABLES/SEQUENCES IN SCHEMA public`` plus
``USAGE ON SCHEMA public`` — and finally drop the role itself, but ONLY if
``pg_shdepend`` shows no remaining cluster-wide dependents.

That ``pg_shdepend`` guard reuses the exact pattern an earlier revision of
migration 0004 used for its (since-removed) ``landlord_sync_role`` — see
that revision's git history for the original writeup. Reason it matters:
roles are CLUSTER-GLOBAL (not per-database). If a sibling database in the
same Postgres cluster also has this migration applied, that other
database's grants still reference ``app_role`` after THIS database's own
``REVOKE`` above finishes, and an unconditional ``DROP ROLE`` would fail
("role ... cannot be dropped because some objects depend on it"), rolling
back this entire downgrade. ``pg_shdepend`` records every shared
(cluster-wide) dependency on a role across ALL databases, so checking it —
not just this database's local state — after our own ``REVOKE`` is the
only reliable "is anything else still using this role" test. When the
guard trips, the harmless NOLOGIN, now-privilege-free role is left in
place rather than the downgrade failing outright — leaving a no-login,
no-privilege role behind is a no-op from a security standpoint.

The step-4 anon/authenticated REVOKEs (both the direct one and the
``ALTER DEFAULT PRIVILEGES`` one) are deliberately NOT reversed here: we
never captured what (if anything) those roles were granted before this
migration ran (Supabase's own project bootstrap grants, not something this
migration created), so there is nothing correct to restore, and
re-opening the Data API bypass channel on downgrade would undo the one
thing rule #2's REVOKE gate most needs to survive. A downgrade takes the
schema back to pre-0005 table/column/constraint shape; it does not — and
should not — reopen a closed security hole.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create app_role + grants, then enable RLS (no FORCE — see module
    docstring) with one policy per table (13 tables — every schema-v1.md
    table except alembic_version)."""

    # ── app_role: NOLOGIN, idempotent via catalog lookup (never pg_has_role) ──
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') THEN
            CREATE ROLE app_role NOLOGIN;
          END IF;
        END $$;
        """
    )
    # Defensive, unconditional (#22 safety review item 10) — see module
    # docstring point 1 for why this guards against a stale LOGIN-enabled
    # app_role surviving a re-migration.
    op.execute("ALTER ROLE app_role NOLOGIN")

    # ── grants: schema usage, ordinary tables, append-only tables ───────────
    op.execute("GRANT USAGE ON SCHEMA public TO app_role")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "landlords, properties, vendors, tenants, cases, message_cases, "
        "drafts, trust_metrics, notifications, push_tokens "
        "TO app_role"
    )
    # APPEND-ONLY (never-break rule #2) — the deferred REVOKE finally lands.
    # SELECT/INSERT is granted first, then UPDATE/DELETE explicitly revoked
    # (a no-op given the grant above never included them) purely for
    # self-documentation — the rule is visible here, not just implied by
    # what was never granted.
    op.execute("GRANT SELECT, INSERT ON messages, audit_log, message_status_events TO app_role")
    op.execute("REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events FROM app_role")

    # ── sequences backing the two bigint IDENTITY primary keys ─────────────
    # Looked up via pg_get_serial_sequence rather than hardcoding
    # "<table>_id_seq" (the default name Postgres happens to choose today,
    # not a documented guarantee for GENERATED ALWAYS AS IDENTITY columns).
    op.execute(
        """
        DO $$
        DECLARE
          seq_name text;
        BEGIN
          seq_name := pg_get_serial_sequence('public.audit_log', 'id');
          IF seq_name IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SEQUENCE %s TO app_role', seq_name);
          END IF;

          seq_name := pg_get_serial_sequence('public.message_status_events', 'id');
          IF seq_name IS NOT NULL THEN
            EXECUTE format('GRANT USAGE ON SEQUENCE %s TO app_role', seq_name);
          END IF;
        END $$;
        """
    )

    # ── RLS: direct landlord_id match (10 tables). ENABLE only, no FORCE —
    # see module docstring "WHY NO FORCE" for the full rationale. ──────────
    op.execute("ALTER TABLE properties ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY properties_isolation ON properties
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE vendors ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY vendors_isolation ON vendors
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenants_isolation ON tenants
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE cases ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY cases_isolation ON cases
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY messages_isolation ON messages
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE drafts ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY drafts_isolation ON drafts
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE trust_metrics ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY trust_metrics_isolation ON trust_metrics
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_log_isolation ON audit_log
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY notifications_isolation ON notifications
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    op.execute("ALTER TABLE push_tokens ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY push_tokens_isolation ON push_tokens
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    # ── RLS: landlords — keyed on id, not landlord_id ───────────────────────
    op.execute("ALTER TABLE landlords ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY landlords_isolation ON landlords
          FOR ALL TO app_role
          USING (id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )

    # ── RLS: message_cases — no landlord_id, EXISTS join through cases ─────
    op.execute("ALTER TABLE message_cases ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY message_cases_isolation ON message_cases
          FOR ALL TO app_role
          USING (
            EXISTS (
              SELECT 1 FROM cases c
              WHERE c.id = message_cases.case_id
                AND c.landlord_id = current_setting('app.current_landlord_id', true)::uuid
            )
          )
          WITH CHECK (
            EXISTS (
              SELECT 1 FROM cases c
              WHERE c.id = message_cases.case_id
                AND c.landlord_id = current_setting('app.current_landlord_id', true)::uuid
            )
          )
        """
    )

    # ── RLS: message_status_events — no landlord_id, EXISTS join through
    # messages (v1.1 table; same treatment as message_cases — see module
    # docstring "WHY THIS COVERS message_status_events TOO") ────────────────
    op.execute("ALTER TABLE message_status_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY message_status_events_isolation ON message_status_events
          FOR ALL TO app_role
          USING (
            EXISTS (
              SELECT 1 FROM messages m
              WHERE m.id = message_status_events.message_id
                AND m.landlord_id = current_setting('app.current_landlord_id', true)::uuid
            )
          )
          WITH CHECK (
            EXISTS (
              SELECT 1 FROM messages m
              WHERE m.id = message_status_events.message_id
                AND m.landlord_id = current_setting('app.current_landlord_id', true)::uuid
            )
          )
        """
    )

    # ── belt-and-braces: close the Supabase Data API bypass channel ────────
    # Guarded: anon/authenticated exist only on live Supabase (PostgREST's
    # roles) — silently skipped on local/CI Postgres. NOT reversed on
    # downgrade (see module docstring "DOWNGRADE"). Two layers: existing
    # tables (direct REVOKE) and future tables this migrating role creates
    # (ALTER DEFAULT PRIVILEGES, #22 safety review item 4).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM authenticated;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop all 13 policies, disable RLS everywhere, revoke app_role's
    grants, and drop app_role itself (guarded, pg_shdepend-aware — see
    module docstring). The anon/authenticated REVOKEs from upgrade() are
    deliberately NOT reversed here (see module docstring "DOWNGRADE")."""

    # ── drop policies (reverse creation order) ──────────────────────────────
    op.execute("DROP POLICY IF EXISTS message_status_events_isolation ON message_status_events")
    op.execute("DROP POLICY IF EXISTS message_cases_isolation ON message_cases")
    op.execute("DROP POLICY IF EXISTS landlords_isolation ON landlords")
    op.execute("DROP POLICY IF EXISTS push_tokens_isolation ON push_tokens")
    op.execute("DROP POLICY IF EXISTS notifications_isolation ON notifications")
    op.execute("DROP POLICY IF EXISTS audit_log_isolation ON audit_log")
    op.execute("DROP POLICY IF EXISTS trust_metrics_isolation ON trust_metrics")
    op.execute("DROP POLICY IF EXISTS drafts_isolation ON drafts")
    op.execute("DROP POLICY IF EXISTS messages_isolation ON messages")
    op.execute("DROP POLICY IF EXISTS cases_isolation ON cases")
    op.execute("DROP POLICY IF EXISTS tenants_isolation ON tenants")
    op.execute("DROP POLICY IF EXISTS vendors_isolation ON vendors")
    op.execute("DROP POLICY IF EXISTS properties_isolation ON properties")

    # ── disable RLS on all 13 tables (explicit statements, not a Python loop
    # over f-strings — ruff S608 flags string-built SQL even when, as here,
    # every interpolated value would be a fixed module-level constant never
    # touched by external input; explicit literals sidestep the false
    # positive and match every other migration's style in this repo) ───────
    op.execute("ALTER TABLE message_status_events DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE message_cases DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE landlords DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE push_tokens DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trust_metrics DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE drafts DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE messages DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cases DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE vendors DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE properties DISABLE ROW LEVEL SECURITY")

    # ── revoke app_role's grants and drop the role (guarded, pg_shdepend-
    # aware — reuses the pattern an earlier revision of migration 0004 used
    # for its since-removed landlord_sync_role; see that revision's git
    # history and the module docstring "DOWNGRADE") ─────────────────────────
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') THEN
            REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_role;
            REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_role;
            REVOKE USAGE ON SCHEMA public FROM app_role;
            IF NOT EXISTS (
              SELECT 1 FROM pg_shdepend
              WHERE refobjid = 'app_role'::regrole
            ) THEN
              DROP ROLE app_role;
            END IF;
          END IF;
        END $$;
        """
    )
