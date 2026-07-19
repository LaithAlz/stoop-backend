"""push_tokens.revoked_at + push_outbox table + RLS (#210 M3)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-18 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.13
amendments block (2026-07-18), which describes exactly what this migration
does and why. Read that block first.

WHY THIS REUSES push_tokens INSTEAD OF A NEW device_tokens TABLE
------------------------------------------------------------------------
``push_tokens`` has existed since migration 0002 (RLS'd since migration
0005) but never had a real writer. #210 M3's own working spec named a new
``device_tokens`` table with an ``expo_push_token`` column — but that name
already exists here, under ``push_tokens.token``, and CLAUDE.md rule 6
("schema names come from schema-v1.md ... never invent a variant") means
the EXISTING name wins. This migration only ADDS what push_tokens was
missing for a real writer (``revoked_at``, for dead-token pruning) and adds
the genuinely new ``push_outbox`` durable-delivery-queue table.

WHAT THIS MIGRATION DOES
-------------------------
1. ``ALTER TABLE push_tokens ADD COLUMN revoked_at timestamptz`` (nullable,
   no default, no backfill — every existing row has none revoked). Set
   ONLY by ``app/push_outbox.py``'s sweep on Expo's ``DeviceNotRegistered``;
   cleared back to ``NULL`` by ``POST /v1/devices``'s upsert (a token
   proven live again). ``platform``'s existing CHECK
   (``'ios','android','web'``) is intentionally left UNCHANGED — see the
   schema doc's v1.13 amendments for why narrowing it wasn't worth the
   churn.

2. ``CREATE TABLE push_outbox`` — one row per (device, event) fan-out,
   following ``notifications``'s own sweep-column pattern EXACTLY
   (``status``/``attempt``/``next_attempt_at``/``payload``/``updated_at``).
   Both FKs (``landlord_id``, ``device_token_id``) are ``ON DELETE
   CASCADE`` — this is best-effort delivery bookkeeping, not an audit
   trail; losing rows when their landlord/device is gone is correct.

3. Role/RLS/grants for ``push_outbox`` — THIS IS THE FIRST MIGRATION SINCE
   0005 TO ADD A GENUINELY NEW TABLE. Migrations 0006-0011 were all
   column/index/CHECK-only amendments; none added a table, so there is no
   more-recent precedent to follow than migration 0005 itself. This
   migration reproduces 0005's exact pattern for this one new table:
   - ``GRANT SELECT, INSERT, UPDATE, DELETE ON push_outbox TO app_role``
     (an ordinary, non-append-only table — same grant shape as
     ``push_tokens`` itself).
   - ``ALTER TABLE push_outbox ENABLE ROW LEVEL SECURITY`` — ``ENABLE``
     only, deliberately NOT ``FORCE`` (identical rationale to every other
     table in migration 0005: the admin/service-role path must stay
     unscoped for the same reasons documented there; ``FORCE`` would do
     nothing for ``app_role``, which is never the table owner).
   - One ``FOR ALL TO app_role`` policy, direct ``landlord_id`` match,
     identical shape to every other direct-landlord_id-keyed table's
     policy in migration 0005.
   The anon/authenticated Data API closure needs NO new statement here:
   migration 0005's ``ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL
   ON TABLES FROM anon, authenticated`` already applies to every table the
   SAME migrating role creates in ``public`` in the future — this table
   included. Re-issuing it here would be redundant, not incremental.

   **LIVE-DRY-RUN FLAG**: this migration is RLS/role-grant-adjacent (it
   enables RLS and grants ``app_role`` on a new table) — per the standing
   rule that any such migration gets a live dry-run against a real
   (non-local) Supabase-shaped database before merge (the same rule
   migration 0005 itself was dry-run under), this migration should get the
   same treatment before it ships. Flagged here for the reviewer/
   orchestrator, not something this migration (or CI) can self-certify.

DOWNGRADE
---------
Reverses upgrade() in dependency-safe order: drop the ``push_outbox``
policy, disable RLS on it, revoke ``app_role``'s grant on it, ``DROP TABLE
push_outbox`` (CASCADE handles its own indexes), then
``ALTER TABLE push_tokens DROP COLUMN revoked_at``. Both steps are always
safe — ``revoked_at`` has no downstream CHECK/index referencing it that a
narrower re-add could conflict with (unlike migration 0009's CHECK
-narrowing hazard), and dropping a brand-new, this-migration-only table
loses no pre-existing data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add push_tokens.revoked_at; create push_outbox with RLS + grants."""

    # ── push_tokens: dead-token marker ──────────────────────────────────────
    op.execute("ALTER TABLE push_tokens ADD COLUMN revoked_at timestamptz")

    # ── push_outbox: durable delivery queue ─────────────────────────────────
    op.execute(
        """
        CREATE TABLE push_outbox (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
          device_token_id uuid NOT NULL REFERENCES push_tokens(id) ON DELETE CASCADE,
          kind            text NOT NULL CHECK (kind IN ('draft_awaiting_approval')),
          payload         jsonb NOT NULL DEFAULT '{}',
          status          text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','sent','failed','exhausted')),
          attempt         integer NOT NULL DEFAULT 0,
          next_attempt_at timestamptz,
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_push_outbox_sweep    ON push_outbox (status, next_attempt_at)")
    op.execute("CREATE INDEX idx_push_outbox_landlord ON push_outbox (landlord_id)")
    op.execute("CREATE INDEX idx_push_outbox_device   ON push_outbox (device_token_id)")

    # ── grant + RLS (mirrors migration 0005's exact pattern — see module
    # docstring "WHAT THIS MIGRATION DOES", point 3) ────────────────────────
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON push_outbox TO app_role")

    op.execute("ALTER TABLE push_outbox ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY push_outbox_isolation ON push_outbox
          FOR ALL TO app_role
          USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
          WITH CHECK (landlord_id = current_setting('app.current_landlord_id', true)::uuid)
        """
    )


def downgrade() -> None:
    """Reverse upgrade(): drop push_outbox's policy/RLS/grant/table, then
    drop push_tokens.revoked_at. See module docstring "DOWNGRADE"."""
    op.execute("DROP POLICY IF EXISTS push_outbox_isolation ON push_outbox")
    op.execute("ALTER TABLE push_outbox DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON push_outbox FROM app_role")
    op.execute("DROP TABLE IF EXISTS push_outbox CASCADE")

    op.execute("ALTER TABLE push_tokens DROP COLUMN revoked_at")
