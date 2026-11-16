"""notifications: ack_token_backup expression index (v1.24)

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-05 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.24
amendment (#289: per-recipient emergency ack tokens).

WHY
---
Before this migration, ``app/agent/emergency_chain.py`` generated exactly
ONE ``ack_token`` per ``emergency_call`` notification and handed the
identical value to the landlord (via ``landlord_sms``) and to the backup
contact (via ``backup_sms``) — one shared secret, two holders. That makes
the token impossible to revoke for a single recipient (removing a backup
contact could not take back a link they already held) and impossible to
attribute (``acknowledge_by_token`` could not tell you it was the backup,
not the landlord, who silenced the chain).

This migration adds the second half of the per-recipient token pair:
``payload ->> 'ack_token_backup'``, sibling to the pre-existing
``payload ->> 'ack_token'`` (migration 0010, v1.9 amendment), which this
revision repurposes as "the landlord's own token" going forward. No new
column or table — same evolution path as migration 0010: the value itself
lives in the existing ``payload`` jsonb column; this migration only adds
an index over it, for the identical reason 0010's own docstring gives
(``GET``/``POST /ack/{token}`` would otherwise sequential-scan the whole
table on every tap of an SMS link) plus the same data-integrity guarantee
(two rows should never share a backup ack token).

SHAPE
-----
``CREATE UNIQUE INDEX uq_notifications_ack_token_backup ON notifications
((payload ->> 'ack_token_backup')) WHERE payload ->> 'ack_token_backup' IS
NOT NULL`` — byte-for-byte the same shape as migration 0010's
``uq_notifications_ack_token``, just for the sibling key. Safe under the
identical NULL-handling every other partial unique index in this schema
already uses: a row with no ``ack_token_backup`` key (every type except
``emergency_call``, and even an ``emergency_call`` row before its first
step is ever claimed — the backup token is minted lazily, by
``_CLAIM_STEP_SQL``'s own healing branch, the first time ANY step of that
chain is claimed, not at INSERT time) extracts SQL ``NULL`` via ``->>``,
and Postgres unique indexes never treat two ``NULL``s as equal.

EXISTING TOKENS (deploy-time compatibility)
--------------------------------------------
A live escalation chain mid-flight when this ships keeps working exactly
as before: its already-minted ``ack_token`` (the pre-migration shared
value) still authenticates BOTH the landlord and whoever already holds a
copy of that link (including a backup contact who received it before this
deploy) — this migration does not, and structurally cannot, retroactively
split a secret that has already been disclosed to two people over SMS. A
fresh, DISTINCT ``ack_token_backup`` is minted automatically the next time
that chain's next step is claimed (within 60s, via the sweep, or sooner if
a step is already due) — see ``app/agent/emergency_chain.py``'s
``_CLAIM_STEP_SQL`` and its module docstring's new "Per-recipient ack
tokens" section for the full account of what this does and does not close
for a pre-deploy chain.

ROUND-TRIP
----------
Unlike migration 0009 (which widens a CHECK constraint and therefore fails
closed on downgrade if disallowed values already exist), there is no
constraint being narrowed here — `downgrade()` simply drops the index.
Always safe: no data loss (the `ack_token_backup` values themselves live
in `payload`, completely untouched by dropping an index over them) — only
a performance regression (backup-token lookups fall back to a sequential
scan until the migration is re-applied), never a correctness one.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ack_token_backup lookup index — see module docstring "SHAPE"."""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_ack_token_backup
          ON notifications ((payload ->> 'ack_token_backup'))
          WHERE payload ->> 'ack_token_backup' IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the index. Always safe — see module docstring "ROUND-TRIP":
    no constraint is being narrowed, so there is no existing-data hazard
    to fail closed against; the underlying `ack_token_backup` payload
    values are untouched either way."""
    op.execute("DROP INDEX IF EXISTS uq_notifications_ack_token_backup")
