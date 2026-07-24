"""unrouted_inbound dead-letter table for unknown-`To` inbound SMS (#170)

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-24 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.17
amendment block (2026-07-24). Read that block first.

WHY
---
The Twilio ``/sms`` webhook (``app/routers/webhooks/twilio.py``) cannot
persist an inbound message whose ``To`` matches no ``properties.
twilio_number`` — ``messages`` has ``NOT NULL landlord_id``/``property_id``
and there is no landlord/property to attach the row to. Before this
migration, that message was simply dropped, with only a loud Sentry alert
(``twilio_sms_unknown_to_number``) as the tide-over — recoverable only via
the Twilio console, and with no local record at all. A misconfigured or
format-mismatched number therefore silently ate tenant messages, including
genuine emergencies — never-break rule #1 territory. This migration adds
the durable, operator-recoverable dead-letter table; the matching webhook
wiring change ships in the same PR (``app/routers/webhooks/twilio.py``).

WHAT THIS MIGRATION DOES
-------------------------
1. ``CREATE TABLE unrouted_inbound`` — see schema-v1.md's v1.17 amendments
   for the full column-by-column rationale. Briefly: ``twilio_sid`` is a
   plain (nullable) ``UNIQUE`` column, same shape as ``messages.
   twilio_sid`` — the ``ON CONFLICT (twilio_sid) DO NOTHING`` target for
   the webhook's idempotent dead-letter insert. ``payload`` carries the
   raw Twilio form fields verbatim (under a ``"form"`` key) plus the Tier-0
   ``PrefilterResult`` computed before the property lookup (under a
   ``"prefilter"`` key) — nested rather than a new column, schema-v1.md
   rule #6's existing "derived signal riding along with a raw jsonb blob"
   evolution path (same shape v1.6 used for ``audit_log.payload``).
   ``resolved_at`` is set manually by a human operator once the underlying
   number/tenant mismatch is fixed — this table is deliberately NOT part of
   the append-only set (rule #2 does not apply; see point 2 for why an
   ``UPDATE`` path here is still safe).

2. **RLS — admin-only, unlike every other table this repo has ever
   migrated.** ``unrouted_inbound`` has no ``landlord_id`` and nothing to
   ``EXISTS``-join through to reach one (no tenant, no case — the whole
   point of this table is that routing failed before either could be
   resolved). Neither migration 0005's ``direct_landlord_id``/``id_keyed``
   shapes nor its ``message_cases``/``message_status_events``
   ``exists_join`` shape apply here. Instead:
   - **No ``GRANT`` to ``app_role`` at all** — every other table in this
     schema grants ``app_role`` at least ``SELECT, INSERT`` (migration
     0005's v1.2-amendments-driven grants); this table grants it nothing,
     so a bare ``SELECT``/``INSERT``/``UPDATE``/``DELETE`` attempted as
     ``app_role`` fails with "permission denied for table" before RLS is
     ever evaluated.
   - **``ENABLE ROW LEVEL SECURITY`` plus exactly ONE policy that denies
     everything unconditionally** — ``FOR ALL TO app_role USING (false)
     WITH CHECK (false)``, named ``unrouted_inbound_isolation`` (same
     ``<table>_isolation`` naming convention every other table's policy
     uses). This is DEFENSE-IN-DEPTH on top of the missing ``GRANT``
     above — even if a future migration mistakenly granted ``app_role`` a
     privilege on this table, no row would ever satisfy ``USING (false)``.
     It also keeps ``tests/test_rls_isolation_matrix.py``'s catalog
     -completeness gate (every table in ``public`` has RLS enabled AND
     exactly one policy) satisfied with no special case in that gate
     itself — only the per-table *behavior* differs (unconditional deny
     vs. GUC-scoped allow), never the *shape* of "RLS enabled, one
     policy."
   - The webhook's own write path uses the ADMIN engine
     (``get_admin_session``, already allowlisted for
     ``app/routers/webhooks/twilio.py`` — migration 0005's module
     docstring, v1.2 amendments point 7), which bypasses RLS entirely
     (``rolbypassrls = TRUE`` for ``postgres``/``service_role``) and needs
     no ``app_role`` grant to write — exactly the same reason the webhook
     can already insert ``messages`` rows for properties/landlords it has
     no session GUC for.
   - Operator reconciliation (setting ``resolved_at``) is a manual,
     admin-engine-only operation — there is no landlord-facing endpoint
     for this table in this migration, and none is implied by it.

DOWNGRADE
---------
Reverses upgrade() in dependency-safe order: drop the deny-all policy,
disable RLS, then ``DROP TABLE unrouted_inbound`` (which drops its own
implicit ``UNIQUE`` index along with it). Always safe — this is a
brand-new, this-migration-only table; there is no pre-existing data a
downgrade could lose beyond what this migration itself created.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create unrouted_inbound; ENABLE RLS with one unconditional-deny
    policy for app_role; grant app_role NOTHING on this table (see module
    docstring, "RLS — admin-only")."""

    op.execute(
        """
        CREATE TABLE unrouted_inbound (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          twilio_sid    text UNIQUE,
          from_number   text NOT NULL,
          to_number     text NOT NULL,
          payload       jsonb NOT NULL,
          received_at   timestamptz NOT NULL DEFAULT now(),
          resolved_at   timestamptz
        )
        """
    )

    # ── admin-only RLS: no GRANT to app_role at all, plus a defense-in-depth
    # unconditional-deny policy (see module docstring point 2) ─────────────
    op.execute("ALTER TABLE unrouted_inbound ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY unrouted_inbound_isolation ON unrouted_inbound
          FOR ALL TO app_role
          USING (false)
          WITH CHECK (false)
        """
    )


def downgrade() -> None:
    """Exactly reverse upgrade(): drop the policy, disable RLS, drop the
    table."""
    op.execute("DROP POLICY IF EXISTS unrouted_inbound_isolation ON unrouted_inbound")
    op.execute("ALTER TABLE unrouted_inbound DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS unrouted_inbound")
