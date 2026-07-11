"""notifications: tenant_ack + degraded_retry types + their dedupe indexes (v1.8)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-11 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.8
amendments block (#109, degraded mode / classification-failure handling).

WHY
---
`docs/02-product/emergency-prefilter.md`'s degraded-mode table needs two
NEW durable artifacts that no existing `notifications.type` value covers:

1. `tenant_ack` (`channel='sms'`) — the templated holding-ack SMS to the
   tenant when classification fails, queued as a durable send-intent for
   #108's future sender to drain. Not `emergency_sms` (a different,
   category-templated safety send) and not `needs_eyes` (reusing it would
   share `uq_notifications_message_dedupe`'s dedupe slot with the
   landlord-facing notification for the SAME message — the two must never
   collide).
2. `degraded_retry` (`channel='push'`, placeholder — never delivered to
   anyone) — an internal-only marker driving the "no keywords at all"
   leg's 1/5/15-minute re-classification retry schedule via the EXISTING
   `next_attempt_at` sweeper-key column. Kept OUT of `needs_eyes`'s own
   type so that a future notification-delivery consumer can treat every
   `needs_eyes` row as unconditionally delivery-ready.

Same evolution path schema-v1.md already documents and this repo has
already used once (v1.1: `messages.party` CHECK extended for 'landlord') —
adding a CHECK value, not a new column or table.

DEDUPE INDEXES
--------------
One partial unique expression index per new type, same NULL-safe pattern
as `uq_notifications_message_dedupe` (migration 0006): a row whose
`payload` has no `message_id` key extracts SQL NULL via `->>`, and
Postgres never treats two NULLs as equal, so only rows that actually carry
a `message_id` are deduplicated — every row this issue's code writes.

ROUND-TRIP
----------
`downgrade()` drops both new indexes first (they reference the CHECK's
values via their partial predicates, though Postgres does not actually
enforce an ordering dependency here — dropped first purely for
readability/symmetry with upgrade()), then restores the narrower CHECK.
Any `tenant_ack`/`degraded_retry` row inserted under this revision would
violate the narrower CHECK on downgrade — acceptable, no writer emits
either value before this migration exists, same acceptance-of-data-risk
note migration 0003's CHECK-narrowing downgrade already established.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen notifications.type to include 'tenant_ack'/'degraded_retry';
    add their per-type dedupe indexes."""
    op.execute("ALTER TABLE notifications DROP CONSTRAINT notifications_type_check")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT notifications_type_check "
        "CHECK (type IN ('emergency_call','emergency_sms','needs_eyes','draft_ready','recap',"
        "'tenant_ack','degraded_retry'))"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_tenant_ack_dedupe
          ON notifications ((payload ->> 'message_id'))
          WHERE type = 'tenant_ack'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_degraded_retry_dedupe
          ON notifications ((payload ->> 'message_id'))
          WHERE type = 'degraded_retry'
        """
    )


def downgrade() -> None:
    """Exactly reverse upgrade(): drop both new indexes, restore the
    narrower CHECK. See module docstring "ROUND-TRIP" for the accepted
    data-loss note."""
    op.execute("DROP INDEX IF EXISTS uq_notifications_tenant_ack_dedupe")
    op.execute("DROP INDEX IF EXISTS uq_notifications_degraded_retry_dedupe")
    op.execute("ALTER TABLE notifications DROP CONSTRAINT notifications_type_check")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT notifications_type_check "
        "CHECK (type IN ('emergency_call','emergency_sms','needs_eyes','draft_ready','recap'))"
    )
