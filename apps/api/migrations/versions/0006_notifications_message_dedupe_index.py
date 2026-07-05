"""notifications: partial unique expression index for cross-process
idempotent emergency_call/needs_eyes dedupe (v1.3)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.3
amendments block (consolidated safety review, #40/#152, item 1).

WHY
---
The Twilio webhook handler (``app/routers/webhooks/twilio.py``) creates
``emergency_call``/``needs_eyes`` notifications idempotently, keyed on
``payload ->> 'message_id'``. An earlier revision used an
application-level ``INSERT ... WHERE NOT EXISTS`` check — NOT safe across
processes/connections: two genuinely concurrent webhook redeliveries of
the same Twilio ``MessageSid`` can each evaluate the ``NOT EXISTS``
condition as true before either commits its own ``INSERT``, so BOTH
insert a notification (duplicate emergency escalations, unbounded under a
replay storm). Reproduced 3/3 with genuinely overlapping transactions
during the consolidated safety review.

This migration adds the one mechanism that IS safe across processes: a
real Postgres unique constraint, enforced by the database itself
regardless of how many concurrent connections race to insert. The
application switches from ``WHERE NOT EXISTS`` to
``INSERT ... ON CONFLICT ((payload ->> 'message_id'), type) WHERE type IN
('emergency_call', 'needs_eyes') DO NOTHING RETURNING id`` in the SAME
change (``app/routers/webhooks/twilio.py``) — the ``ON CONFLICT`` target
above is Postgres's "unique index inference" syntax and must reproduce
this index's expression list and partial predicate verbatim to be
inferred correctly.

PARTIAL + EXPRESSION INDEX, NULL SEMANTICS
-------------------------------------------
- Partial (``WHERE type IN ('emergency_call', 'needs_eyes')``): only
  these two notification types are deduplicated; ``emergency_sms``,
  ``draft_ready``, and ``recap`` rows are entirely unaffected and may
  repeat freely, exactly as before this migration.
- Expression (``(payload ->> 'message_id')``): ``payload`` is jsonb with
  no dedicated ``message_id`` column (schema-v1.md rule #6 — never invent
  a column when the existing jsonb payload already carries the value);
  the expression extracts the text value for indexing.
- NULL: a row whose ``payload`` has no ``message_id`` key extracts SQL
  ``NULL`` via ``->>``. Ordinary SQL NULL semantics mean a Postgres unique
  index NEVER treats two NULLs as equal, so any number of such rows
  coexist without colliding — the dedupe key only ever constrains rows
  that actually carry a ``message_id``, which is every ``emergency_call``/
  ``needs_eyes`` row the webhook handler writes.

DURABILITY
----------
``emergency_call``/``needs_eyes`` notifications are the durable
idempotency anchor this index (and the webhook's ``ON CONFLICT``
inference) depends on — see schema-v1.md's v1.3 amendments note: they
must NEVER be deleted. A future retention/archival job must exclude
``notifications`` rows of these two types (or exclude ``notifications``
entirely).

ROUND-TRIP
----------
``downgrade()`` drops the index; existing rows are test-only at this
revision (the live table is empty in every real deployment so far), so no
data-loss note is needed the way migration 0003's column DROP required
one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the partial unique expression index closing the cross-process
    concurrency hole (consolidated safety review, #40/#152 item 1)."""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_message_dedupe
          ON notifications ((payload ->> 'message_id'), type)
          WHERE type IN ('emergency_call', 'needs_eyes')
        """
    )


def downgrade() -> None:
    """Drop the index. No data-loss note needed: the index carries no data
    of its own, and the underlying `notifications` rows are untouched."""
    op.execute("DROP INDEX IF EXISTS uq_notifications_message_dedupe")
