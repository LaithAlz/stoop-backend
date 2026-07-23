"""properties: address-dedupe partial UNIQUE index (v1.15, #203 item 2)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-23 00:00:00.000000

**NUMBERING COLLISION FLAG — read before merging.** This revision claims
"0013" / schema-v1.md "v1.15" (current head at the time this was written).
Sibling lanes (#122/#170/#213) may ALSO be claiming migration "0013" (and/or
a "v1.15" schema-doc amendment) concurrently on their own branches — same
collision class schema-v1.md's own v1.11/v1.14 amendments already document
("needing no migration does NOT make a doc-heading amendment number
uncontended on its own"; "renumbered from v1.10 — PR #202 took that label
first"). Whoever merges SECOND must renumber (this file's `revision`/
`down_revision`, and the doc heading) to the next free slot — this is not
optional cleanup, it is how `down_revision` chains stay a single line.

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.15
amendment (2026-07-23, #203 item 2 — the #53 provisioning safety re-review's
"cheap, high-value" follow-up. #203's OWN numbering: item 1 is the
durable-intent-row/M4 structural fix, evaluated and DEFERRED to its own
follow-up issue; item 3 is the global-spend-bound follow-up, also not this
one).

WHY
---
``POST /v1/properties`` (``app/routers/properties.py``) has always run a
pre-check SELECT (``_DUPLICATE_PROPERTY_SQL``) before ever calling Twilio,
to stop a client's timeout-and-retry from buying a SECOND number for what
is, from the landlord's perspective, the same property. That pre-check is
a genuine TOCTOU race: two truly concurrent requests for the same
normalized address can both pass it (neither sees the other's still
-uncommitted insert) and both purchase a real, billed Twilio number before
either's INSERT runs — the #53 safety re-review accepted this as a
bounded, self-healing residual (2x at most, capped, never touches tenancy
isolation or the emergency line) rather than blocking on it.

This migration closes the DB-level half of that race with a genuine
constraint: a partial-in-spirit (see "NOT ACTUALLY PARTIAL" below) UNIQUE
INDEX mirroring ``_DUPLICATE_PROPERTY_SQL``'s own normalization EXACTLY —
same four columns (``landlord_id``, ``lower(trim(address_line1))``,
``lower(trim(city))``, ``lower(trim(province))``), same exclusion of
``postal_code`` (nullable/optional; a retry that omits it the second time
must still collide as a duplicate). ``app/routers/properties.py``'s
``create_property`` now catches the resulting ``IntegrityError`` on this
specific constraint and routes it through the SAME compensation path as
any other post-purchase DB failure (release the just-purchased number via
the existing ``release_number_best_effort`` seam — no new Twilio call
site) — but returns the ORDINARY 409 ``duplicate_property`` response
instead of paging Sentry, since a race the schema itself now serializes is
an expected, self-healing outcome, not a bug.

NOT ACTUALLY PARTIAL (naming note)
-----------------------------------
The issue that requested this (#203) calls it a "partial UNIQUE INDEX" by
analogy with every OTHER dedupe index in this file (0006/0009/0010/0011),
all of which need a ``WHERE`` clause because they share one polymorphic
``notifications`` table across many unrelated ``type`` values. ``properties``
has no such polymorphism and — per its own module docstring in
schema-v1.md — no ``deleted_at``/soft-delete column to exclude either
(``DELETE /v1/properties/{id}`` is a genuine hard delete, unchanged by
this migration). So this index has no ``WHERE`` clause at all: every LIVE
row is already exactly the population that must be unique. Functionally
identical guarantee, textually a plain (if expression-based) UNIQUE INDEX
rather than a partial one — noted here so a future reader doesn't go
looking for a ``WHERE`` clause that was never needed.

ROUND-TRIP HAZARD
-----------------
``CREATE UNIQUE INDEX`` validates against every EXISTING row at creation
time, same as every CHECK-constraint-narrowing migration in this file
(0009/0011) — if two committed ``properties`` rows for the SAME landlord
already collide on this normalized key (structurally shouldn't happen
today; the pre-check has been in place since #53, and the cap bounds any
landlord's blast radius to 25), ``upgrade()`` FAILS CLOSED (raises, rolls
back, stays at the prior revision) rather than silently picking a winner
and leaving the other row live-but-now-invisible to future dedupe
protection. An operator hitting this must resolve the collision manually
(rename/merge/delete one of the two rows) before retrying the migration —
same remediation shape as 0009/0011's own hazard notes, just surfaced by
Postgres's own index-build validation rather than a CHECK's.

``downgrade()`` — plain ``DROP INDEX``, no data loss, no hazard: unlike
0009/0011's CHECK-narrowing downgrades, dropping a UNIQUE index never
destroys rows, only the constraint itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the landlord-scoped, normalized-address UNIQUE index. FAILS
    CLOSED (raises, rolls back) if any two LIVE ``properties`` rows for the
    same landlord already collide on this key — see module docstring
    "ROUND-TRIP HAZARD"."""
    op.execute(
        """
        CREATE UNIQUE INDEX uq_properties_landlord_address_dedupe
          ON properties (
            landlord_id,
            lower(trim(address_line1)),
            lower(trim(city)),
            lower(trim(province))
          )
        """
    )


def downgrade() -> None:
    """Exactly reverse upgrade(): drop the index. Always safe -- see module
    docstring "ROUND-TRIP HAZARD"."""
    op.execute("DROP INDEX IF EXISTS uq_properties_landlord_address_dedupe")
