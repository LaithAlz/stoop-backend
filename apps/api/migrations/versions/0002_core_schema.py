"""core schema — properties, vendors, tenants, cases, messages, message_cases,
drafts, trust_metrics, audit_log, notifications, push_tokens

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-26 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md (lines 34-245).
Every table, column, type, DEFAULT, CHECK, UNIQUE, FK (with ON DELETE action),
and CREATE INDEX is reproduced verbatim from that document.

APPEND-ONLY TABLES
------------------
``messages`` and ``audit_log`` are append-only per never-break rule #2.
The REVOKE UPDATE, DELETE enforcement (schema-v1.md line 149 / 215) is
DEFERRED to the Supabase role/RLS phase (#22/#3) because the app role
(``authenticated`` / ``service_role``) does not exist in local Postgres.
No code writes UPDATE/DELETE to these tables; the REVOKE MUST land before
any writer ships.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all remaining v1 tables per schema-v1.md."""

    # ── properties ────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE properties (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          label           text NOT NULL,
          address_line1   text NOT NULL,
          city            text NOT NULL,
          province        text NOT NULL DEFAULT 'ON',
          postal_code     text,
          lat             double precision,
          lon             double precision,
          twilio_number   text UNIQUE,
          twilio_sid      text,
          house_rules     text,
          quiet_hours     jsonb NOT NULL DEFAULT '{"start":"21:00","end":"08:00"}',
          heating_season  jsonb NOT NULL DEFAULT '{"start":"09-15","end":"06-01"}',
          backup_contact  jsonb,
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_properties_landlord ON properties (landlord_id)")
    op.execute("CREATE INDEX idx_properties_twilio   ON properties (twilio_number)")

    # ── vendors ───────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE vendors (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id   uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          name          text NOT NULL,
          trade         text NOT NULL
                        CHECK (trade IN ('plumbing','electrical','hvac','appliance',
                                         'locksmith','pest','general','other')),
          phone         text NOT NULL,
          notes         text,
          working_hours jsonb,
          active        boolean NOT NULL DEFAULT true,
          created_at    timestamptz NOT NULL DEFAULT now(),
          updated_at    timestamptz NOT NULL DEFAULT now(),
          UNIQUE (landlord_id, phone)
        )
        """
    )
    op.execute("CREATE INDEX idx_vendors_landlord ON vendors (landlord_id)")

    # ── tenants ───────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE tenants (
          id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id         uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          property_id         uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
          name                text,
          phone               text NOT NULL,
          unit                text,
          vulnerable_occupant text
                              CHECK (vulnerable_occupant IN ('infant','elderly','medical_device')),
          notes               text,
          active              boolean NOT NULL DEFAULT true,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now(),
          UNIQUE (property_id, phone)
        )
        """
    )
    op.execute("CREATE INDEX idx_tenants_phone    ON tenants (phone)")
    op.execute("CREATE INDEX idx_tenants_landlord ON tenants (landlord_id)")

    # ── cases ─────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE cases (
          id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id         uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          property_id         uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
          tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
          vendor_id           uuid REFERENCES vendors(id),
          status              text NOT NULL DEFAULT 'open'
                              CHECK (status IN ('open','awaiting_approval','awaiting_tenant',
                                                'resolved','reopened')),
          resolved_reason     text
                              CHECK (resolved_reason
                                     IN ('landlord','tenant_confirmed','auto_stale')),
          severity            text
                              CHECK (severity IN ('emergency','urgent','routine')),
          intent              text,
          title               text,
          langgraph_thread_id text UNIQUE NOT NULL,
          related_case_id     uuid REFERENCES cases(id),
          emergency_fired_at  timestamptz,
          last_activity_at    timestamptz NOT NULL DEFAULT now(),
          resolved_at         timestamptz,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_cases_queue    ON cases (landlord_id, status, severity)")
    op.execute("CREATE INDEX idx_cases_tenant   ON cases (tenant_id, status)")
    op.execute("CREATE INDEX idx_cases_activity ON cases (status, last_activity_at)")

    # ── messages (APPEND-ONLY) ────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE messages (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
          tenant_id       uuid REFERENCES tenants(id),
          vendor_id       uuid REFERENCES vendors(id),
          case_id         uuid REFERENCES cases(id),
          direction       text NOT NULL CHECK (direction IN ('inbound','outbound')),
          party           text NOT NULL CHECK (party IN ('tenant','vendor')),
          body            text NOT NULL,
          media           jsonb,
          twilio_sid      text UNIQUE,
          twilio_status   text,
          prefilter       jsonb,
          classification  jsonb,
          tokens_in       integer,
          tokens_out      integer,
          model           text,
          llm_cost_cents  numeric(10,4),
          sms_cost_cents  numeric(10,4),
          created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_messages_case    ON messages (case_id, created_at)")
    op.execute("CREATE INDEX idx_messages_channel ON messages (tenant_id, created_at)")
    # APPEND-ONLY (never-break rule #2): REVOKE UPDATE, DELETE ON messages FROM the app role
    # is DEFERRED to the Supabase role/RLS phase (#22/#3) — the role doesn't exist in local
    # Postgres. No code writes UPDATE/DELETE to this table; the REVOKE MUST land before any
    # writer ships.

    # ── message_cases ─────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE message_cases (
          message_id uuid NOT NULL REFERENCES messages(id),
          case_id    uuid NOT NULL REFERENCES cases(id),
          PRIMARY KEY (message_id, case_id)
        )
        """
    )

    # ── drafts ────────────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE drafts (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id       uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          case_id           uuid NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
          recipient         text NOT NULL CHECK (recipient IN ('tenant','vendor')),
          body              text NOT NULL,
          prompt_version    text NOT NULL,
          status            text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','stale','approved','sending',
                                              'sent','rejected','cancelled')),
          auto_send         boolean NOT NULL DEFAULT false,
          scheduled_send_at timestamptz,
          sent_message_id   uuid REFERENCES messages(id),
          edited            boolean NOT NULL DEFAULT false,
          final_body        text,
          created_at        timestamptz NOT NULL DEFAULT now(),
          updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_drafts_one_pending ON drafts (case_id) WHERE status = 'pending'"
    )
    op.execute("CREATE INDEX idx_drafts_queue ON drafts (landlord_id, status)")

    # ── trust_metrics ─────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE trust_metrics (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id       uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          property_id       uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
          severity          text NOT NULL CHECK (severity IN ('emergency','urgent','routine')),
          clean_approvals   integer NOT NULL DEFAULT 0,
          edited_approvals  integer NOT NULL DEFAULT 0,
          rejections        integer NOT NULL DEFAULT 0,
          consecutive_clean integer NOT NULL DEFAULT 0,
          autonomy_unlocked boolean NOT NULL DEFAULT false,
          unlocked_at       timestamptz,
          revoked_at        timestamptz,
          updated_at        timestamptz NOT NULL DEFAULT now(),
          UNIQUE (property_id, severity)
        )
        """
    )

    # ── audit_log (APPEND-ONLY) ───────────────────────────────────────────────
    # NOTE: landlord_id and case_id are plain uuid with NO FOREIGN KEY — intentional
    # so audit records survive deletes of the referenced rows.
    op.execute(
        """
        CREATE TABLE audit_log (
          id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          landlord_id uuid NOT NULL,
          case_id     uuid,
          actor       text NOT NULL CHECK (actor IN ('agent','landlord','system','prefilter')),
          action      text NOT NULL CHECK (action IN (
                        'message_received','classified','case_opened','case_reopened',
                        'case_resolved','drafted','draft_stale','approved','edited',
                        'rejected','sent','send_cancelled','auto_sent',
                        'emergency_triggered','emergency_call_attempt','acknowledged',
                        'vendor_engaged','degraded_mode','trust_unlocked','trust_revoked',
                        'billing_changed','settings_changed')),
          payload     jsonb NOT NULL DEFAULT '{}',
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_audit_case     ON audit_log (case_id, created_at)")
    op.execute("CREATE INDEX idx_audit_landlord ON audit_log (landlord_id, created_at)")
    # APPEND-ONLY (never-break rule #2): REVOKE UPDATE, DELETE ON audit_log FROM the app role
    # is DEFERRED to the Supabase role/RLS phase (#22/#3) — the role doesn't exist in local
    # Postgres. No code writes UPDATE/DELETE to this table; the REVOKE MUST land before any
    # writer ships.

    # ── notifications ─────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE notifications (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
          case_id         uuid REFERENCES cases(id),
          type            text NOT NULL CHECK (type IN ('emergency_call','emergency_sms',
                            'needs_eyes','draft_ready','recap')),
          channel         text NOT NULL CHECK (channel IN ('voice','sms','push','email')),
          status          text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','sent','acknowledged','failed','exhausted')),
          attempt         integer NOT NULL DEFAULT 0,
          next_attempt_at timestamptz,
          acknowledged_at timestamptz,
          payload         jsonb NOT NULL DEFAULT '{}',
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_notifications_sweep ON notifications (status, next_attempt_at)")

    # ── push_tokens ───────────────────────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE push_tokens (
          id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          landlord_id  uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
          token        text NOT NULL UNIQUE,
          platform     text NOT NULL CHECK (platform IN ('ios','android','web')),
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop all 11 tables in reverse FK-dependency order. CASCADE handles
    self-refs and index cleanup."""
    op.execute("DROP TABLE IF EXISTS push_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS notifications CASCADE")
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS trust_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS drafts CASCADE")
    op.execute("DROP TABLE IF EXISTS message_cases CASCADE")
    op.execute("DROP TABLE IF EXISTS messages CASCADE")
    op.execute("DROP TABLE IF EXISTS cases CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
    op.execute("DROP TABLE IF EXISTS vendors CASCADE")
    op.execute("DROP TABLE IF EXISTS properties CASCADE")
