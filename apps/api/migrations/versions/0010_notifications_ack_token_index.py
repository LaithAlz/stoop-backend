"""notifications: ack_token expression index (v1.9)

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.9
amendment (#108 safety review, 2026-07-12, finding 8, LOW/doc-first).

WHY
---
The emergency escalation chain (`app/agent/emergency_chain.py`) generates
a random, unguessable `ack_token` (`secrets.token_urlsafe(24)`) into an
`emergency_call` row's `payload` at T+0, and looks it up on every
`GET`/`POST /ack/{token}` request (the tokenized SMS-link acknowledgment
surface, `app/routers/notifications.py`). Without an index, every one of
those lookups is a sequential scan over the entire `notifications` table.
No new column or table — the token itself already lives in the existing
`payload` jsonb column; this migration only adds an index over it, same
evolution path as every other dedupe index in this file (migrations 0006,
0009).

SHAPE
-----
`CREATE UNIQUE INDEX uq_notifications_ack_token ON notifications
((payload ->> 'ack_token')) WHERE payload ->> 'ack_token' IS NOT NULL`.

UNIQUE doubles as a data-integrity guarantee (two rows should never share
the same token — a collision would let one tenant's chain be acknowledged
via a different chain's link). Safe under the same NULL-handling Postgres
already uses for every other partial unique index in this schema: a row
with no `ack_token` key (every type except `emergency_call`, and even
`emergency_call` rows before `handle_emergency_trigger` enriches them)
extracts SQL `NULL` via `->>`, and Postgres unique indexes never treat two
`NULL`s as equal — those rows never collide with each other or with a
real token. The `WHERE ... IS NOT NULL` partial predicate additionally
keeps the index itself small.

ROUND-TRIP
----------
Unlike migration 0009 (which widens a CHECK constraint and therefore
fails closed on downgrade if disallowed values already exist), there is no
constraint being narrowed here — `downgrade()` simply drops the index.
This is ALWAYS safe: no data loss (the `ack_token` values themselves live
in `payload`, completely untouched by dropping an index over them) — only
a performance regression (token lookups fall back to a sequential scan
until the migration is re-applied), never a correctness one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ack_token lookup index — see module docstring "SHAPE"."""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_ack_token
          ON notifications ((payload ->> 'ack_token'))
          WHERE payload ->> 'ack_token' IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the index. Always safe — see module docstring "ROUND-TRIP":
    no constraint is being narrowed, so there is no existing-data hazard
    to fail closed against; the underlying `ack_token` payload values are
    untouched either way."""
    op.execute("DROP INDEX IF EXISTS uq_notifications_ack_token")
