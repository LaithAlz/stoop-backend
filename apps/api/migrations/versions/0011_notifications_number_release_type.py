"""notifications: number_release type + its dedupe index (v1.11)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-13 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.11
amendment (#53, property provisioning; renumbered from v1.10 after PR
#202's `cases.severity` amendment took that label first).

WHY
---
Deprovisioning (`DELETE /v1/properties/{id}`) needs a durable, sweep
-visible record of "release this Twilio number back to the pool" that
survives past the moment the `properties` row itself is hard-deleted (that
table has no `deleted_at`/status column to hang a grace period off, unlike
`tenants`/`vendors`' `active` flag) — `apps/api/CLAUDE.md`'s "windows are
data, not sleeps" doctrine, mirroring the approve-flow's `scheduled_send_at`
undo window. Same evolution path as migrations 0006/0009 (adding a
`notifications.type` CHECK value, not a new column/table) and the SAME
`idx_notifications_sweep (status, next_attempt_at)` index those types
already share — no new sweep index needed here.

`channel='push'` reuses the existing "internal marker, never actually
delivered to a person" convention `degraded_retry` already established
(migration 0009) — there is no real delivery channel for "release a phone
number," and `channel` has no NULL/none option in its CHECK.

DEDUPE INDEX
------------
`uq_notifications_number_release_dedupe` — same NULL-safe partial-unique
pattern as every other dedupe index in this file, but keyed on
`payload ->> 'twilio_sid'` rather than `message_id` (this type has no
`message_id` at all). Guards a narrow but real race: two concurrent
`DELETE` requests for the same property could each read the row's
`twilio_sid` before either commits the delete; this makes scheduling the
release idempotent regardless of which one actually wins the delete race.

ROUND-TRIP
----------
Same hazard shape as migration 0009: `downgrade()` restoring the narrower
CHECK FAILS CLOSED (raises, rolls back, stays at head) if any
`number_release` row currently exists — `ALTER TABLE ... ADD CONSTRAINT
... CHECK (...)` validates against every existing row at ALTER time. An
operator who genuinely needs to roll back past this revision with live
rows must resolve them first, as an explicit, out-of-migration action
(same remediation note as 0009's own docstring) — this migration
deliberately does not delete them itself for the same reason 0009 doesn't:
a schema downgrade silently destroying a live, not-yet-actioned release
record is a worse failure mode than the migration simply refusing to run.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen notifications.type to include 'number_release'; add its
    twilio_sid-keyed dedupe index."""
    op.execute("ALTER TABLE notifications DROP CONSTRAINT notifications_type_check")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT notifications_type_check "
        "CHECK (type IN ('emergency_call','emergency_sms','needs_eyes','draft_ready','recap',"
        "'tenant_ack','degraded_retry','number_release'))"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_number_release_dedupe
          ON notifications ((payload ->> 'twilio_sid'))
          WHERE type = 'number_release'
        """
    )


def downgrade() -> None:
    """Exactly reverse upgrade(): drop the new index, restore the narrower
    CHECK. FAILS CLOSED (raises, rolls back, does not run) if any
    `number_release` row currently exists — see module docstring
    "ROUND-TRIP" for the full hazard analysis and the manual remediation an
    operator must perform first."""
    op.execute("DROP INDEX IF EXISTS uq_notifications_number_release_dedupe")
    op.execute("ALTER TABLE notifications DROP CONSTRAINT notifications_type_check")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT notifications_type_check "
        "CHECK (type IN ('emergency_call','emergency_sms','needs_eyes','draft_ready','recap',"
        "'tenant_ack','degraded_retry'))"
    )
