"""Canonicalize existing phone-bearing columns to E.164 (#232 data migration)

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-03 00:00:00.000000

Canonical schema source: docs/03-engineering/schema-v1.md — the v1.21
amendment block (2026-08-03). Read that block first.

WHY
---
schema-v1.md has always documented ``landlords.phone``, ``properties.
twilio_number``, the ``phone`` key inside ``properties.backup_contact``,
``tenants.phone``, and ``vendors.phone`` as E.164 — but before this
migration (and its accompanying application-code change, same PR — see
``app/phone.py`` and every router it's wired into), nothing ever enforced
that at write time. A landlord who typed ``"(416) 555-0134"`` or
``"416-555-0134"`` into a form BEFORE this PR's server-side validation
shipped has that exact string sitting in the database today. Two
consequences, both closed by the code half of this PR going forward, but
NEITHER of which retroactively fixes an already-stored row:

1. **#260** — a malformed ``landlords.phone``/``vendors.phone``/``tenants.
   phone`` reaches Twilio's ``create_call``/``send_sms`` verbatim and fails
   (error 21211), which ``app/agent/emergency_chain.py``'s
   ``_execute_action`` degrades to a silent ``status='failed'`` by design
   — the landlord's phone (or a vendor's, or a tenant's outbound reply)
   simply never rings/receives, with no visible error anywhere.
2. **#232** — the Twilio ``/sms`` webhook's routing match (``app/routers/
   webhooks/twilio.py``) now canonicalizes the INBOUND ``From``/``To``
   before comparing (this PR's code change), but that only helps if the
   STORED ``properties.twilio_number``/``tenants.phone`` value is ALSO
   canonical — comparing a canonicalized inbound value against a
   still-uncanonical stored one is just as broken as comparing two raw
   values. This migration is what makes the stored side trustworthy.

WHAT THIS MIGRATION DOES
-------------------------
For each of the five phone-bearing locations above, in `upgrade()`:
1. SELECT every row with a non-null (non-blank, for ``backup_contact``)
   value.
2. Compute ``app.phone.to_e164(value)`` — the SAME function (imported
   directly, not re-implemented — one canonicalization authority, see
   that module's own docstring) every write-time endpoint now validates
   through.
3. If the canonical form differs from the stored value, ``UPDATE`` that
   one row to the canonical form.
4. If ``to_e164`` returns ``None`` (uncanonicalizable), the row is
   **left completely untouched** — see "UNCANONICALIZABLE ROWS" below.

Each table's before/after counts (rows scanned / rows updated / rows left
uncanonicalizable / rows skipped as collisions, see "COLLISIONS" below) are
logged via ``structlog`` — **counts and ids only, never the phone values
themselves** (never-break rule #5, which explicitly also applies to
migrations).

COLLISIONS — SKIPPED, NEVER LET A UNIQUE VIOLATION ABORT THE MIGRATION
------------------------------------------------------------------------
**Safety review, 2026-08-03, finding 2 — SHOULD-FIX.** Three of the five locations this migration
touches are UNIQUE-constrained: ``properties.twilio_number`` (globally
unique), ``tenants`` (``UNIQUE (property_id, phone)``), ``vendors``
(``UNIQUE (landlord_id, phone)``). Two rows whose stored values differ
only in FORMATTING — the single most likely shape of pre-existing dirty
data — canonicalize to the SAME target value. Blindly ``UPDATE``-ing both
would raise ``UniqueViolationError`` mid-loop (same class as migration
0016's own pre-existing-duplicates edge case, documented in that
migration's docstring) — INSIDE Alembic's transactional DDL, so the ENTIRE
migration rolls back and ``alembic upgrade head`` fails outright.
Additionally, Postgres's own unique-violation ``DETAIL`` text embeds the
colliding value (``Key (phone)=(+1416...) already exists``) — letting that
reach a driver exception, migration stderr, or (worse) a caller that
interpolates it into a message would be a rule-#5 violation (a phone
number in migration/CI output).

Closed by grouping BEFORE issuing any ``UPDATE``, not by catching the
``IntegrityError`` after the fact: every row's canonical value is computed
first, then grouped by ``(scope, canonical_value)`` — ``scope`` is the
constraint's own scoping column (``property_id`` for ``tenants``,
``landlord_id`` for ``vendors``, a constant for the globally-unique
``properties.twilio_number`` and the unconstrained ``landlords.phone``).
ANY group with more than one row — including a row that's already
independently stored in its canonical form, if some OTHER row's
canonicalized value collides with it — is skipped ENTIRELY (not one
"winner" picked; adjudicating which of several differently-formatted
strings is the "real" number is an operator judgment call, never a
migration's). This makes a ``UniqueViolationError`` from this migration's
own writes structurally unreachable — every ``UPDATE`` this migration
issues targets a canonical value no OTHER row (touched or untouched) in
the same scope also maps to. Skipped rows are logged as
``collision_skipped=<count>`` plus their row ids (``collision_row_ids`` —
ids only, never values, same rule-#5 discipline as everywhere else in this
migration) so an operator can find and manually adjudicate them — the
same "logged, not auto-resolved" posture as the uncanonicalizable-row
handling below. ``tenants``/``vendors``' scoping is intentionally an
over-approximation for the (unconstrained) ``landlords.phone`` and the
(globally-unique) ``properties.twilio_number`` cases — both simply use a
single constant scope, which is exactly correct for a global-uniqueness
column and harmlessly conservative for a column with no uniqueness
constraint at all (it can only ever cause a false-positive "collision"
skip, never a missed one, and ``landlords.phone`` has no constraint to
violate regardless). ``properties.backup_contact.phone`` carries no
uniqueness constraint at all (it is one key inside a ``jsonb`` blob, not
its own column) — no collision handling is needed or applied there.

UNCANONICALIZABLE ROWS — LEFT AS-IS, NEVER NULLED, NEVER DELETED
------------------------------------------------------------------------
A row this migration cannot canonicalize is left with its EXACT original
value, untouched. This is a deliberate choice, not an oversight:

- **Nulling it would be actively worse than leaving it broken.** A
  malformed ``landlords.phone`` at least preserves the DIGITS an operator
  or the landlord themselves can manually fix (e.g. via a future "clear
  phone" affordance, or a support ticket); a ``NULL`` erases that
  information entirely and additionally changes observable behavior
  elsewhere (``emergency_chain.py`` treats a ``NULL`` landlord phone as
  "skip this action, no landlord to call" — silently REMOVING an
  emergency-chain step is strictly worse than leaving a step that fails
  loudly-ish via Twilio's own error, since #260's write-time validation
  means this population can now only ever shrink, never grow).
- **Deleting the row is not on the table at all** — these are not
  standalone rows this migration owns; they are `landlords`/`properties`/
  `tenants`/`vendors` rows with FKs and history attached. Never-break
  rule #1's spirit (the emergency line is never gated) extends here: a
  malformed-but-present contact detail must never be silently made to
  disappear.
- **No deployed environment has real traffic yet** (live Supabase carries
  none — same "acceptable by construction" precedent migration 0016's own
  docstring already establishes for its own pre-existing-duplicates edge
  case) — so in practice this migration is expected to canonicalize
  everything or nothing, never leave a real landlord stranded. The
  uncanonicalizable-row handling above is defense-in-depth for whatever
  test/seed data exists today and for correctness on principle, not a
  response to a known production incident.
- **Operator follow-up**: the logged per-table "left uncanonicalizable"
  count is how an operator would notice and adjudicate any real row this
  ever affects — by design, no automated action beyond logging is taken.

IDEMPOTENT / SAFE TO RE-RUN
----------------------------
Re-running ``upgrade()`` (e.g. after a ``downgrade`` back to 0016, as the
round-trip test does) is a no-op the second time for every row it already
canonicalized: ``to_e164(already_canonical_value) == already_canonical_
value``, so no ``UPDATE`` is issued and no row is double-counted as
"updated". Uncanonicalizable rows are, by definition, unaffected by
however many times this runs.

DOWNGRADE
---------
A deliberate **no-op**. Reversing "make this value canonical" has no
well-defined inverse — there is no way to recover which of the
(potentially many) non-canonical strings that canonicalize to the same
E.164 value was originally stored, and there is no product reason to want
that reversal (going BACK to an undialable value is never correct). This
migration issues no DDL at all (no new column/constraint/index — see
schema-v1.md's v1.21 amendment: the invariant it states was already true
of every column's declared type, `text`; only the ENFORCEMENT is new, and
that lives in application code, not the schema), so there is no schema
state for `downgrade()` to reverse either. `downgrade()` exists only so
Alembic's revision chain and this repo's "down/up must round-trip"
convention (`apps/api/CLAUDE.md`) are satisfied: `upgrade()` then
`downgrade()` then `upgrade()` again leaves the database in the exact
same (fully canonicalized, wherever canonicalization was possible) state
either way — a true no-op round-trip, not a lossy one, precisely because
canonicalization is idempotent and downgrade doesn't undo it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.phone import to_e164

log = structlog.get_logger(__name__)

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Simple text columns: landlords.phone, properties.twilio_number,
# tenants.phone, vendors.phone — same shape, one shared helper.
#
# The third tuple element is the UNIQUE constraint's own scoping column
# (``None`` = a single, constant scope — correct for both the globally
# -unique ``properties.twilio_number`` and the unconstrained ``landlords.
# phone``; see the module docstring's "COLLISIONS" section for why an
# unconstrained column being treated as one global scope is harmless).
# ---------------------------------------------------------------------------

_SIMPLE_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("landlords", "phone", None),
    ("properties", "twilio_number", None),
    ("tenants", "phone", "property_id"),
    ("vendors", "phone", "landlord_id"),
)

# A single, constant scope value for the two columns above with no
# per-row scoping column (see ``_SIMPLE_COLUMNS``'s own comment).
_GLOBAL_SCOPE = "__global__"


def _canonicalize_simple_column(
    bind: Connection, *, table: str, column: str, scope_column: str | None
) -> None:
    """Canonicalize every non-null value in ``<table>.<column>`` — see
    module docstring, including "COLLISIONS" for the grouping logic below.
    ``table``/``column``/``scope_column`` are always one of the fixed,
    hardcoded triples in ``_SIMPLE_COLUMNS`` above (never request/row
    data), so building the SQL text with an f-string here is safe (no
    injection surface) — the alternative (four fully duplicated
    near-identical functions) was rejected as needless repetition for
    genuinely hand-written, migration-local SQL.
    """
    scope_select = f", {scope_column} AS scope" if scope_column is not None else ""
    select_stmt = (
        f"SELECT id, {column} AS value{scope_select} "  # noqa: S608
        f"FROM {table} WHERE {column} IS NOT NULL"
    )
    select_sql = text(select_stmt)
    update_sql = text(f"UPDATE {table} SET {column} = :value WHERE id = :id")  # noqa: S608

    rows = bind.execute(select_sql).mappings().all()

    original_by_id: dict[Any, str] = {}
    scope_by_id: dict[Any, Any] = {}
    canonical_by_id: dict[Any, str] = {}
    unparseable = 0
    for row in rows:
        row_id = row["id"]
        value: str = row["value"]
        original_by_id[row_id] = value
        scope_by_id[row_id] = row["scope"] if scope_column is not None else _GLOBAL_SCOPE
        canonical = to_e164(value)
        if canonical is None:
            unparseable += 1
            continue
        canonical_by_id[row_id] = canonical

    # Group by (scope, canonical value) BEFORE issuing any UPDATE — see
    # module docstring, "COLLISIONS". A group with more than one row means
    # two (or more) stored values collapse to the SAME canonical target
    # within the SAME uniqueness scope; every row in that group is skipped,
    # not just the "extra" ones, since there is no safe way to pick a
    # winner here.
    ids_by_key: dict[tuple[Any, str], list[Any]] = {}
    for row_id, canonical in canonical_by_id.items():
        key = (scope_by_id[row_id], canonical)
        ids_by_key.setdefault(key, []).append(row_id)

    updated = 0
    collision_skipped = 0
    collision_row_ids: list[str] = []
    for (_scope, canonical), ids in ids_by_key.items():
        if len(ids) > 1:
            collision_skipped += len(ids)
            collision_row_ids.extend(str(row_id) for row_id in ids)
            continue
        row_id = ids[0]
        if canonical != original_by_id[row_id]:
            bind.execute(update_sql, {"id": row_id, "value": canonical})
            updated += 1

    log.info(
        "phone_canonicalization_backfill",
        table=table,
        column=column,
        scanned=len(rows),
        updated=updated,
        left_uncanonicalizable=unparseable,
        collision_skipped=collision_skipped,
        collision_row_ids=collision_row_ids,
    )


# ---------------------------------------------------------------------------
# properties.backup_contact — a {name, phone} jsonb blob; only the "phone"
# key is a phone number, so only that key is touched, in place.
# ---------------------------------------------------------------------------

_SELECT_BACKUP_CONTACT_PHONE = text(
    """
    SELECT id, backup_contact ->> 'phone' AS value
    FROM properties
    WHERE backup_contact ? 'phone'
      AND length(trim(backup_contact ->> 'phone')) > 0
    """
)

_UPDATE_BACKUP_CONTACT_PHONE = text(
    """
    UPDATE properties
    SET backup_contact = jsonb_set(backup_contact, '{phone}', to_jsonb(CAST(:value AS text)))
    WHERE id = :id
    """
)


def _canonicalize_backup_contact_phone(bind: Connection) -> None:
    rows = bind.execute(_SELECT_BACKUP_CONTACT_PHONE).mappings().all()
    updated = 0
    unparseable = 0
    for row in rows:
        value: str = row["value"]
        canonical = to_e164(value)
        if canonical is None:
            unparseable += 1
            continue
        if canonical != value:
            bind.execute(_UPDATE_BACKUP_CONTACT_PHONE, {"id": row["id"], "value": canonical})
            updated += 1

    log.info(
        "phone_canonicalization_backfill",
        table="properties",
        column="backup_contact.phone",
        scanned=len(rows),
        updated=updated,
        left_uncanonicalizable=unparseable,
    )


def upgrade() -> None:
    """Canonicalize every existing phone-bearing value this migration can
    confidently canonicalize; leave anything it cannot exactly as-is (see
    module docstring, "UNCANONICALIZABLE ROWS"), and never let two rows
    colliding on the same canonical value abort the migration (see
    "COLLISIONS")."""
    bind: Connection = op.get_bind()

    # Cheap insurance (safety review, 2026-08-03, finding 5 — LOW; the
    # #231 lesson: an unbounded scan+update should never be able to sit
    # blocked indefinitely behind a lock this migration doesn't otherwise
    # need) — fails fast and loudly instead of hanging the deploy if some
    # other session is unexpectedly holding a conflicting lock on one of
    # these tables.
    bind.execute(text("SET LOCAL lock_timeout = '3s'"))

    for table, column, scope_column in _SIMPLE_COLUMNS:
        _canonicalize_simple_column(bind, table=table, column=column, scope_column=scope_column)
    _canonicalize_backup_contact_phone(bind)


def downgrade() -> None:
    """Deliberate no-op — see module docstring, "DOWNGRADE"."""
