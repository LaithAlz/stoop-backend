"""cases: pending_resolved_at (tenant-confirmed resolution timer, v1.5)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.5
amendments block (#110).

WHY
---
conversation-model.md's tenant-confirmed-resolution phase ("agent detects
'all fixed', *proposes* resolution -- landlord-visible, auto-applies after
48h if not contradicted") has no representable state without a durable,
timer-shaped marker: `cases.status` has no pending/proposed value, and
`audit_log.action` has no matching vocabulary entry (only the terminal
`case_resolved`). Discovered as a genuine schema-vocabulary gap during
#110's implementation and closed here, per the repo's own "the undo window
is data, not a sleep" precedent (see `drafts.scheduled_send_at`) rather
than inventing a second implicit in-memory timer.

DESIGN CHOICE -- APPLY-AT time, not proposal time
--------------------------------------------------
`pending_resolved_at` stores the MOMENT the resolution auto-applies
(`propose_resolution` sets it to `now() + 48h` directly), not the moment
the tenant confirmed. See schema-v1.md's v1.5 amendments block, point 2,
for the full rationale (self-describing column name, trivial sweep query,
window changes need no migration).

NULLABLE, NO DEFAULT, NO BACKFILL
----------------------------------
Every existing `cases` row has no proposal pending -- `NULL` is the correct
value for 100% of rows at migration time, no backfill needed. No CHECK
constraint is added: any timestamp value is legal (the application, not the
database, decides when to set/clear it), mirroring `resolved_at`'s own
lack of a CHECK in this same table.

ROUND-TRIP
----------
`downgrade()` drops the column. No data-loss note in the schema-v1.md sense
migration 0003's column DROP required (`messages.twilio_status` carried
real historical data) -- this column is new, so a downgrade after any
`upgrade` in a real deployment WOULD lose in-flight proposal state, but
that is the ordinary, expected cost of downgrading past a migration that
added a column with live data in it; nothing special to call out beyond
what `resolved_at`/`emergency_fired_at`/every other nullable timestamptz
column on this table already implies.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable `pending_resolved_at` timer column to `cases`."""
    op.execute("ALTER TABLE cases ADD COLUMN pending_resolved_at timestamptz")


def downgrade() -> None:
    """Drop the column. See module docstring "ROUND-TRIP"."""
    op.execute("ALTER TABLE cases DROP COLUMN pending_resolved_at")
