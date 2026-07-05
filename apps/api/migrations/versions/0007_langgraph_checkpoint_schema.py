"""LangGraph checkpoint schema (#24) — dedicated, RLS-free, admin-only

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.4
amendments block (#24), closing the forward note in the v1.2 amendments
block's point 5 and migration 0005's module docstring point 5, both of
which promised this treatment ahead of time: LangGraph's checkpoint tables
(``AsyncPostgresSaver.setup()``, ``app/agent/checkpointer.py``) live in a
DEDICATED ``langgraph`` schema, reached only via that module's own psycopg
connection pool (built directly from ``settings.database_url``, the
admin/service-role connection string) — never ``app_role``, never RLS.

Why a separate schema instead of a 14th ``public`` table
------------------------------------------------------------------------
``tests/test_rls_isolation_matrix.py``'s
``test_no_tables_outside_descriptor_set_exist_in_public_schema`` (#23)
enforces that every table in ``public`` either has a ``TableDescriptor`` +
an RLS policy, or is deliberately excluded via a separate, non-``public``
schema reached only by the admin engine — that test's own docstring names
both options explicitly. This migration chooses the second: the
checkpoint tables never exist in ``public`` at all, so that test's catalog
scan (``relnamespace = 'public'::regnamespace``) never sees them and stays
green BY CONSTRUCTION, with no new ``TableDescriptor``/RLS policy/allowlist
entry required anywhere.

WHAT THIS MIGRATION DOES
-------------------------
1. ``CREATE SCHEMA IF NOT EXISTS langgraph`` — idempotent natively (unlike
   ``CREATE ROLE``, which migration 0005 had to guard with a ``pg_roles``
   catalog lookup, ``CREATE SCHEMA`` supports ``IF NOT EXISTS`` directly).
2. Belt-and-braces REVOKEs (defense in depth, not closing a real hole: a
   schema this migration just created has no privilege granted to anyone
   but its owner by default — Postgres does NOT auto-grant ``USAGE``/
   ``CREATE`` on a freshly created non-``public`` schema to ``PUBLIC`` the
   way ``initdb`` does for the special ``public`` schema, which is exactly
   why migration 0005 needed an explicit REVOKE there and this schema does
   not strictly need one). Written anyway, mirroring migration 0005's
   Supabase Data API closure verbatim, so the "no grants, anywhere, ever"
   invariant is documented and machine-enforced rather than merely assumed
   from Postgres defaults:
   - ``REVOKE ALL ON SCHEMA langgraph FROM PUBLIC`` — the implicit
     ``PUBLIC`` pseudo-role that always exists.
   - ``REVOKE ALL ON SCHEMA langgraph FROM app_role`` — ``app_role``
     already exists by this point in the migration chain (created by
     0005).
   - Guarded ``anon``/``authenticated`` REVOKEs (PostgREST roles that
     exist only on live Supabase; silently skipped on local/CI Postgres),
     both the direct schema REVOKE and an ``ALTER DEFAULT PRIVILEGES`` for
     tables the migrating role creates in this schema in the future —
     mirroring migration 0005's two-layer closure for ``public`` exactly.
     ``ALTER DEFAULT PRIVILEGES`` (no ``FOR ROLE`` clause, so it targets
     ``CURRENT_USER``) matters here specifically: the checkpoint TABLES
     themselves are created later, by ``AsyncPostgresSaver.setup()``,
     using the SAME admin/service role that is running this migration (via
     ``settings.database_url``) — so this default-privileges REVOKE, set
     up now, is what actually reaches those not-yet-existing tables once
     ``setup()`` creates them.
3. No tables are created here. ``AsyncPostgresSaver.setup()`` creates and
   migrates its own four unqualified tables (``checkpoints``,
   ``checkpoint_blobs``, ``checkpoint_writes``, ``checkpoint_migrations``)
   idempotently, the first time ``app/agent/checkpointer.py``'s
   ``setup_checkpointer()`` runs (wired into ``app/main.py``'s lifespan) —
   see that module's docstring for why table creation belongs to the
   library's own runtime ``.setup()`` call rather than Alembic DDL: the
   library owns its own migration/version table (``checkpoint_migrations``)
   and versioning scheme; running its DDL through Alembic would fight that
   rather than compose with it, and ``setup()`` is itself idempotent and
   safe to call on every process start.

DOWNGRADE
---------
``DROP SCHEMA IF EXISTS langgraph CASCADE`` — safe: nothing outside the
checkpoint tables (which only ever exist inside this schema) lives here,
and there is no FK either direction between ``langgraph`` and ``public``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the dedicated ``langgraph`` schema; grant it to no one."""
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

    # Belt-and-braces (see module docstring point 2) — PUBLIC and app_role
    # never get any privilege on this schema.
    op.execute("REVOKE ALL ON SCHEMA langgraph FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA langgraph FROM app_role")

    # Guarded: anon/authenticated (PostgREST roles) exist only on live
    # Supabase — silently skipped locally/CI, same pattern as migration
    # 0005's Data API bypass closure for the public schema.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON SCHEMA langgraph FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON SCHEMA langgraph FROM authenticated;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            ALTER DEFAULT PRIVILEGES IN SCHEMA langgraph REVOKE ALL ON TABLES FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            ALTER DEFAULT PRIVILEGES IN SCHEMA langgraph REVOKE ALL ON TABLES FROM authenticated;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the schema (and, if any exist, its checkpoint tables) entirely."""
    op.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
