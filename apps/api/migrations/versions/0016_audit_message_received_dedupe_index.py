"""Partial unique expression index for message_received dedupe (#184 item 4).

schema-v1.md v1.20 amendment implements this. ``graph_entry.py``'s
``message_received`` audit write used an ``INSERT ... SELECT ... WHERE NOT
EXISTS`` — a shape this codebase itself documents as NOT cross-process-safe
(two genuinely concurrent transactions each evaluate ``NOT EXISTS`` as true
before either commits; the exact class migration 0006 closed for
``notifications``). The consequence here was only ever a cosmetic duplicate
audit row (``message_received`` stopped gating anything in the #34 round-2
fix), but the audit trail is a product surface (the LTB artifact), and the
honest fix was already named in that module's own docstring: a real unique
index + ``ON CONFLICT``.

Same pattern as 0006: partial + expression, so ONLY ``message_received``
rows are constrained — every other action may repeat freely, and rows
whose payload lacks a ``message_id`` key extract SQL ``NULL``, which
Postgres unique indexes never treat as equal.

NO live-Supabase dry-run required: this migration touches no roles, no
grants, no RLS — only an index on a table whose append-only REVOKEs
(migration 0005) are untouched. (The dry-run rule covers role/grant/RLS
migrations specifically — CLAUDE.md / stoop-change-control rule 10.)

Pre-existing duplicates would fail the CREATE UNIQUE INDEX: acceptable by
construction — no deployed environment exists (live Supabase carries no
traffic), local/CI DBs migrate from scratch, and a hypothetical operator
hitting it should adjudicate the duplicates rather than have a migration
silently delete audit rows (rule #2's spirit: migrations never DELETE from
audit_log).

Down/up round-trips: downgrade drops the index only — no data change,
same "performance regression, never a correctness one" shape as 0010's
ack-token index.
"""

from __future__ import annotations

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: None = None
depends_on: None = None

_CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX uq_audit_message_received_dedupe
  ON audit_log ((payload ->> 'message_id'))
  WHERE action = 'message_received'
"""

_DROP_INDEX_SQL = "DROP INDEX IF EXISTS uq_audit_message_received_dedupe"


def upgrade() -> None:
    op.execute(_CREATE_INDEX_SQL)


def downgrade() -> None:
    op.execute(_DROP_INDEX_SQL)
