"""create landlords table

Revision ID: 0001
Revises:
Create Date: 2026-06-24 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md
This migration implements exactly the landlords table as defined there —
no columns added, none omitted, types and constraints identical.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the landlords table per schema-v1.md."""
    op.execute(
        """
        CREATE TABLE landlords (
          id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          auth_user_id        uuid NOT NULL UNIQUE,
          email               text NOT NULL,
          full_name           text,
          phone               text,
          timezone            text NOT NULL DEFAULT 'America/Toronto',
          voice_profile       jsonb,
          price_cohort        text NOT NULL DEFAULT 'early_access'
                              CHECK (price_cohort IN ('early_access','standard')),
          subscription_tier   text NOT NULL DEFAULT 'free'
                              CHECK (subscription_tier IN ('free','full','desk')),
          subscription_status text NOT NULL DEFAULT 'none'
                              CHECK (subscription_status IN
                                ('none','active','past_due','canceled')),
          stripe_customer_id  text UNIQUE,
          deleted_at          timestamptz,
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Drop the landlords table."""
    op.execute("DROP TABLE IF EXISTS landlords")
