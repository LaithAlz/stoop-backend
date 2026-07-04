"""message_status_events (APPEND-ONLY, v1.1); messages.party += 'landlord';
drop messages.twilio_status

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.1
amendments block (changelog dated 2026-07-04) and the
``message_status_events`` section. Every table, column, type, DEFAULT,
CHECK, and CREATE INDEX below is reproduced verbatim from that document.

This migration implements three v1.1 amendments (#151):

1. New append-only table ``message_status_events`` — Twilio
   delivery-status callbacks append here instead of ever touching
   ``messages`` (rule #2: messages is append-only). Delivery state is
   derived by strict status precedence (see schema-v1.md), never by
   recency; duplicates are appended as facts — no UNIQUE, no upsert.

2. ``messages.party`` CHECK relaxed from ``('tenant','vendor')`` (shipped
   in 0002) to ``('tenant','vendor','landlord')`` — approve-by-SMS (#122)
   landlord replies must be representable. 'landlord' rows are
   command-channel messages: never forwarded to tenants/vendors, excluded
   from tenant-conversation queries. Postgres auto-named the inline CHECK
   from 0002 ``messages_party_check`` (confirmed against the deployed
   schema); it is dropped and re-added explicitly by that name.

3. ``messages.twilio_status`` dropped — deprecated in v1.1, superseded by
   ``message_status_events``, and never written after INSERT. Safe to
   drop: no writer exists yet (this migration MUST land before #40 ships
   the send path).

APPEND-ONLY TABLES
------------------
``message_status_events`` is append-only per never-break rule #2, same as
``messages``/``audit_log``. The REVOKE UPDATE, DELETE enforcement
(schema-v1.md, message_status_events section) is DEFERRED to the Supabase
role/RLS phase (#22/#3) — the app role (``authenticated`` / ``service_role``)
does not exist in local Postgres, exactly the same deferred gate 0002
documented for ``messages``/``audit_log``. No code writes UPDATE/DELETE to
this table; the REVOKE MUST land before any writer ships.

DOWNGRADE DATA LOSS
--------------------
``downgrade()`` re-adds ``twilio_status`` as a plain nullable ``text``
column with no data — the dropped column's historical values are gone.
This is acceptable and standard for a dropped column with no writer.
``downgrade()`` also restores the narrower ``messages_party_check``
(``'tenant','vendor'`` only); any ``'landlord'`` rows inserted under 0003
would violate that narrower CHECK on downgrade. This is acceptable because
no writer emits ``'landlord'`` rows yet (#122 hasn't shipped).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create message_status_events; relax messages.party; drop twilio_status."""

    # ── message_status_events (APPEND-ONLY, v1.1) ────────────────────────────
    op.execute(
        """
        CREATE TABLE message_status_events (
          id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          message_id  uuid NOT NULL REFERENCES messages(id),
          status      text NOT NULL
                      CHECK (status IN ('accepted','queued','sending','sent','delivered',
                                        'undelivered','failed')),
          error_code  text,
          payload     jsonb NOT NULL DEFAULT '{}',
          created_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # APPEND-ONLY (never-break rule #2): REVOKE UPDATE, DELETE ON message_status_events FROM
    # the app role is DEFERRED to the Supabase role/RLS phase (#22/#3) — the role doesn't exist
    # in local Postgres. No code writes UPDATE/DELETE to this table; the REVOKE MUST land
    # before any writer ships.
    op.execute(
        "CREATE INDEX idx_message_status_events_message "
        "ON message_status_events (message_id, created_at)"
    )

    # ── messages.party: relax CHECK to include 'landlord' (v1.1, #122) ───────
    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_party_check")
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT messages_party_check "
        "CHECK (party IN ('tenant','vendor','landlord'))"
    )

    # ── messages.twilio_status: drop (deprecated v1.1, no writer) ────────────
    op.execute("ALTER TABLE messages DROP COLUMN twilio_status")


def downgrade() -> None:
    """Exactly reverse upgrade(): restore twilio_status (no data), restore
    the narrower party CHECK, drop message_status_events."""

    # ── messages.twilio_status: restore as plain nullable text (data loss —
    # the dropped column's values are gone; acceptable, no writer exists) ────
    op.execute("ALTER TABLE messages ADD COLUMN twilio_status text")

    # ── messages.party: restore narrower CHECK (any 'landlord' rows would
    # violate it — acceptable, no writer emits 'landlord' rows yet) ──────────
    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_party_check")
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT messages_party_check "
        "CHECK (party IN ('tenant','vendor'))"
    )

    # ── message_status_events: drop ───────────────────────────────────────────
    op.execute("DROP TABLE IF EXISTS message_status_events CASCADE")
