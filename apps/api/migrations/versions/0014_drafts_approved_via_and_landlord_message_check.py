"""drafts.approved_via + messages landlord-null CHECK (#122, approve-by-SMS)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-23 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.16
amendment block (2026-07-23). Read that block first.

**Numbering note, resolved at rebase:** this migration originally claimed
``0013``/schema-v1.md ``v1.15``, but sibling lane #203 item 2 (the
properties address-dedupe unique index) claimed that same slot first and
merged as PR #227. Per the "whoever merges SECOND renumbers" precedent
this file's own prior draft already documented (same collision class as
migration 0011's "renumbered from v1.10 — PR #202 took that label first"),
this migration is renumbered to ``0014``/``v1.16`` here, chaining after
``0013`` (properties address-dedupe) via ``down_revision = "0013"``.

WHAT THIS MIGRATION DOES
-------------------------
1. ``ALTER TABLE drafts ADD COLUMN approved_via text CHECK (...)`` — nullable,
   no default, no backfill (every existing row was approved, if at all,
   before this column existed, or was never approved at all). Set by
   ``app/agent/nodes/finalize_draft_decision.py::apply_approve_or_edit``
   going forward: ``'dashboard'`` for the pre-existing
   ``POST /v1/drafts/{id}/approve``/``edit-and-send`` path (no behavior
   change, just labeled), ``'sms'`` for the new approve-by-SMS reply
   handler (#122). Never set by ``auto_send.py``'s ``apply_auto_send`` —
   auto-send is a distinct mechanism from either human-approval channel.

2. ``ALTER TABLE messages ADD CONSTRAINT messages_landlord_party_null_check``
   — DB-level enforcement of the "landlord command-channel rows carry
   ``tenant_id``/``vendor_id`` NULL" invariant (documented since v1.1, never
   enforced at the database level until now — PR #154 senior review,
   pinned forward on issue #122). Every row this codebase has ever written
   already satisfies this (the webhook's own `_INSERT_MESSAGE_SQL` sets
   ``tenant_id = NULL`` whenever ``party = 'landlord'``, and vendor_id has
   never been written by that INSERT at all), so this ADD CONSTRAINT is
   safe against existing data with no need for a NOT VALID / VALIDATE
   split.

DOWNGRADE
---------
Reverses upgrade() in dependency-safe order: drop the new messages CHECK
constraint, then drop drafts.approved_via. Both steps lose no pre-existing
data beyond the column/constraint itself — ``approved_via`` has no
downstream FK/index referencing it that a drop could conflict with.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add drafts.approved_via; add the messages landlord-null CHECK."""

    # ── drafts: which human channel approved this row ───────────────────────
    op.execute(
        "ALTER TABLE drafts ADD COLUMN approved_via text "
        "CHECK (approved_via IN ('dashboard', 'sms'))"
    )

    # ── messages: DB-level enforcement of the landlord-row-NULL invariant ───
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT messages_landlord_party_null_check "
        "CHECK (party <> 'landlord' OR (tenant_id IS NULL AND vendor_id IS NULL))"
    )


def downgrade() -> None:
    """Exactly reverse upgrade(): drop the messages CHECK, then
    drafts.approved_via."""

    op.execute("ALTER TABLE messages DROP CONSTRAINT messages_landlord_party_null_check")
    op.execute("ALTER TABLE drafts DROP COLUMN approved_via")
