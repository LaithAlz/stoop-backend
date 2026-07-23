"""Properties CRUD (#54).

Every endpoint here is landlord-scoped via ``Depends(require_landlord)`` —
the RLS ``app.current_landlord_id`` GUC (migration 0005) plus an explicit
``landlord_id = :landlord_id`` predicate on every query (belt-and-braces
per ``apps/api/CLAUDE.md``'s "every multi-tenant query scoped by
landlord_id" convention). A property id that exists but belongs to a
different landlord is indistinguishable from one that doesn't exist at all
— both resolve to 404 ``property_not_found``, never leaking cross-tenant
existence.

Shapes match ``docs/03-engineering/api-contracts.md``'s "Properties"
section exactly. Column names are ``schema-v1.md``'s ``properties`` table,
verbatim.

Twilio provisioning (#53, ``app/property_provisioning.py`` +
``app/integrations/twilio_provision.py``): ``POST /v1/properties``
provisions a real Twilio number BEFORE inserting the row — search (area
code → province → any CA number) → purchase → configure webhooks →
best-effort A2P association — and only inserts the row once that succeeds,
with ``twilio_number``/``twilio_sid`` populated from the start (never a
row with them ``NULL`` on success; schema-v1.md's "null until provisioned"
comment describes the failure/pending state, not the success path). Any
failure releases a just-purchased number as compensation before surfacing
a clean error — see ``property_provisioning.py``'s own docstring and
api-contracts.md's v1.12 amendment for the full failure-code contract.

**Pre-Twilio-call money guards (safety review, 2026-07-13, finding H1)** —
TWO cheap DB reads run BEFORE ``provision_number`` is ever called (before
any real-money Twilio call), neither an entitlement/paywall gate (rule #1:
the emergency line is never paywalled — these apply identically to every
landlord, free or paid):

1. **Property cap** — 409 ``property_limit_reached`` once a landlord
   already has ``settings.max_properties_per_landlord`` properties. A pure
   spend/abuse guard against a buggy or malicious client hammering this
   endpoint, not a plan limit.
2. **Duplicate-address dedupe** — 409 ``duplicate_property`` if the SAME
   landlord already has a property at the same normalized address
   (``address_line1``/``city``/``province``, case/whitespace-insensitive —
   see ``_DUPLICATE_PROPERTY_SQL``). This is what makes a client's
   timeout-and-retry hit the dedupe check instead of buying a SECOND
   number for what is, from the landlord's perspective, the same property.
   Mirrors the existing ``duplicate_phone`` convention (tenants/vendors) in
   spirit.

   **#203 item 2 (closing the DB-level race)** — this pre-check alone is a
   TOCTOU SELECT: two genuinely concurrent creates for the same address
   could both pass it before either committed, both purchasing a real
   Twilio number (the #53 safety re-review accepted this as a bounded,
   self-healing 2x-at-most residual, never touching tenancy isolation or
   the emergency line). Migration 0013 (schema-v1.md v1.15) now backs this
   with a genuine landlord-scoped, normalized-address UNIQUE index
   (``uq_properties_landlord_address_dedupe``) mirroring
   ``_DUPLICATE_PROPERTY_SQL``'s own normalization EXACTLY. The pre-check
   SELECT above still runs first (it stays the fast, cheap path that stops
   a serial retry before ever calling Twilio) — but a request that slips
   past it via a genuine race now hits the unique index at INSERT time
   instead: see ``_is_duplicate_property_unique_violation``/
   ``create_property``'s own ``IntegrityError`` handling below, which
   routes that specific violation through the SAME
   ``release_number_best_effort`` compensation seam as any other
   post-purchase failure, then returns the ordinary 409
   ``duplicate_property`` — no Sentry page, since a race the schema itself
   now serializes is an expected, self-healing outcome, not a bug.

**Connection-pinning tradeoff (safety review finding M4) — read before
touching this handler.** ``create_property`` holds its
``require_landlord`` session/transaction (and therefore the pooled DB
connection and the RLS GUC) open across up to ~40s of real Twilio HTTP
calls (three REQUIRED sequential requests at up to 10s each — search,
purchase, configure — plus a 4th best-effort A2P call, also up to 10s).
This is a genuine tradeoff, not an oversight: ``require_landlord``'s own
module docstring already forbids a mid-handler ``session.commit()``
(``SET LOCAL`` — the GUC — dies with the transaction), so there is no
cheap way to release the connection mid-request without a structural
redesign (a durable "provisioning intent" row + background worker,
matching the notifications-sweep pattern this same module already uses
for deprovisioning). That redesign is **#203 item 1** — evaluated
alongside #203 item 2 (this docstring's "Duplicate-address dedupe" note
above) and explicitly DEFERRED to its own future issue: large, competes
with the scheduler's existing sweeps, and needs its own reconcile design
(see that issue and PR #204's senior-review comment on the go-live
pool-separation gate) — not this PR's scope. Mitigated today by: (a)
running the cap/dedupe pre-checks (cheap reads) BEFORE any Twilio call,
so a request that's going to be rejected never holds the connection
through a Twilio round trip at all; (b) the property cap itself
also bounds the WORST CASE (a single landlord can only ever hold this
pattern open `max_properties_per_landlord` times in a row before hitting
the cap); (c) each Twilio call already has its own 10s timeout
(``twilio_provision.py``). Flagged here explicitly for senior review to
adjudicate whether this tradeoff is acceptable at current traffic, or
whether the structural fix should be pulled forward.

``DELETE`` deprovisions: requires ``?confirm=true`` (400
``confirmation_required`` otherwise, checked before the existing
``has_open_cases``/``has_dependents`` checks below), then — for a property
that had a live number — schedules a 24h-grace-period release rather than
calling Twilio synchronously (``property_provisioning.schedule_number_
release``; the actual release is swept by ``app/scheduler.py``). See
api-contracts.md's v1.12 amendment.

``DELETE`` is a genuine hard delete (``properties`` has no ``deleted_at``
column, unlike ``tenants``/``vendors``' ``active`` flag) — the documented
``has_open_cases`` 409 is the first-line business check. A property that
survives that check but still has FK-referencing rows is caught as a
second-line ``IntegrityError`` and surfaced as a clean 409
``has_dependents`` rather than a raw 500 — a contract addition proposed in
the same PR (see ``api-contracts.md``'s Properties section, "DELETE"
note). The explicit ``ON DELETE RESTRICT`` columns targeting
``properties(id)`` (schema-v1.md) are ``tenants.property_id``,
``cases.property_id``, ``messages.property_id``, and
``trust_metrics.property_id``.

Audit trail (#54 AC: "audit entries on changes that affect agent
behavior"): a ``PATCH`` that actually changes ``house_rules`` writes an
``audit_log`` row (``actor='landlord'``, ``action='settings_changed'``) —
compared against the pre-update value so a no-op PATCH (same value resent)
never writes a spurious entry. The AC's OTHER agent-behavior-affecting
field, "voice-profile fields," lives on ``landlords.voice_profile``
(schema-v1.md) — not a ``properties`` column at all — so it is satisfied
by ``PATCH /v1/me`` (``app/routers/me.py``), not by this router; see that
module's own audit-trail note.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import property_provisioning
from app.agent.case_lifecycle import OPEN_STATUSES
from app.audit import record_audit_log
from app.config import settings
from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursorError,
    decode_cursor,
    paginate_rows,
)
from app.validation import reject_explicit_null

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["properties"])

_OPEN_STATUSES_LIST = sorted(OPEN_STATUSES)

# ---------------------------------------------------------------------------
# Response / request models — shapes from api-contracts.md's Properties
# section, field names from schema-v1.md's `properties` table.
# ---------------------------------------------------------------------------


class PropertyResponse(BaseModel):
    id: UUID
    label: str
    address_line1: str
    city: str
    province: str
    postal_code: str | None
    twilio_number: str | None
    house_rules: str | None
    quiet_hours: dict[str, Any]
    heating_season: dict[str, Any]
    backup_contact: dict[str, Any] | None
    open_case_count: int
    created_at: datetime


class PropertyListResponse(BaseModel):
    items: list[PropertyResponse]
    next_cursor: str | None


class PropertyCreateRequest(BaseModel):
    """See api-contracts.md's Properties section. ``area_code`` (#53) is a
    TRANSIENT Twilio-provisioning hint only — never persisted (no
    ``properties`` column for it, per that section's v1.12 amendment): a
    3-digit NANP area code biasing the number search; falls back to the
    property's own ``province``, then any available Canadian number."""

    label: str
    address_line1: str
    city: str
    province: str | None = None
    postal_code: str | None = None
    house_rules: str | None = None
    backup_contact: dict[str, Any] | None = None
    area_code: str | None = Field(default=None, pattern=r"^\d{3}$")


class PropertyUpdateRequest(BaseModel):
    label: str | None = None
    address_line1: str | None = None
    city: str | None = None
    province: str | None = None
    postal_code: str | None = None
    house_rules: str | None = None
    backup_contact: dict[str, Any] | None = None
    quiet_hours: dict[str, Any] | None = None
    heating_season: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# p.twilio_sid rides along for delete_property's deprovisioning step (#53)
# — never surfaced in PropertyResponse (_row_to_property doesn't reference
# it), same as any other internal-only column would be.
_PROPERTY_COLUMNS = (
    "p.id, p.label, p.address_line1, p.city, p.province, p.postal_code, "
    "p.twilio_number, p.twilio_sid, p.house_rules, p.quiet_hours, p.heating_season, "
    "p.backup_contact, p.created_at, COALESCE(oc.open_case_count, 0) AS open_case_count"
)

# landlord_id filter here is belt-and-braces (senior review, A2): the
# outer query already scopes `p` to this landlord, so a case row from a
# different landlord could never join to it via `oc.property_id = p.id`
# anyway — but explicit is the established convention everywhere else.
_OPEN_CASE_COUNT_JOIN = (
    "LEFT JOIN ("
    "SELECT property_id, COUNT(*) AS open_case_count FROM cases "
    "WHERE status = ANY(:open_statuses) AND landlord_id = :landlord_id GROUP BY property_id"
    ") oc ON oc.property_id = p.id"
)

_SELECT_ONE_SQL = text(
    f"SELECT {_PROPERTY_COLUMNS} FROM properties p "  # noqa: S608
    f"{_OPEN_CASE_COUNT_JOIN} "
    "WHERE p.id = :id AND p.landlord_id = :landlord_id"
)

_INSERT_SQL = text(
    """
    INSERT INTO properties (landlord_id, label, address_line1, city, province,
                             postal_code, house_rules, backup_contact,
                             twilio_number, twilio_sid)
    VALUES (:landlord_id, :label, :address_line1, :city, COALESCE(:province, 'ON'),
            :postal_code, :house_rules, CAST(:backup_contact AS jsonb),
            :twilio_number, :twilio_sid)
    RETURNING id
    """
)

# #53 safety review H1 — both pre-checks below run BEFORE any Twilio call.
_COUNT_LANDLORD_PROPERTIES_SQL = text(
    "SELECT COUNT(*) FROM properties WHERE landlord_id = :landlord_id"
)

# Natural key for "the same property" (module docstring, "Duplicate-address
# dedupe"): landlord_id + case/whitespace-insensitive address_line1/city
# /province. postal_code is deliberately EXCLUDED (nullable/optional — a
# retry that omits it the second time must still be recognized as the same
# duplicate address).
_DUPLICATE_PROPERTY_SQL = text(
    """
    SELECT id FROM properties
    WHERE landlord_id = :landlord_id
      AND LOWER(TRIM(address_line1)) = LOWER(TRIM(:address_line1))
      AND LOWER(TRIM(city)) = LOWER(TRIM(:city))
      AND LOWER(TRIM(province)) = LOWER(TRIM(:province))
    LIMIT 1
    """
)

_COUNT_OPEN_CASES_SQL = text(
    "SELECT COUNT(*) FROM cases "
    "WHERE property_id = :id AND landlord_id = :landlord_id AND status = ANY(:open_statuses)"
)

_DELETE_SQL = text("DELETE FROM properties WHERE id = :id AND landlord_id = :landlord_id")

# #203 item 2 — migration 0013's uq_properties_landlord_address_dedupe is the
# DB-level backstop for the TOCTOU pre-check above (_DUPLICATE_PROPERTY_SQL).
# Same detection pattern as app/routers/webhooks/twilio.py's own
# _is_ack_token_collision: constraint_name first, a substring fallback across
# driver/version differences in whether it's populated -- WIDENED (safety
# re-review finding 2) with an extra nested-cause lookup and two gates on
# the fallback, see _is_duplicate_property_unique_violation's own docstring.
_DUPLICATE_PROPERTY_CONSTRAINT_NAME = "uq_properties_landlord_address_dedupe"
_UNIQUE_VIOLATION_SQLSTATE = "23505"


def _is_duplicate_property_unique_violation(exc: IntegrityError) -> bool:
    """``True`` iff *exc* is a UNIQUE VIOLATION on
    ``uq_properties_landlord_address_dedupe`` specifically — never swallows
    any OTHER integrity error (e.g. a genuine schema/FK problem, or the
    pre-existing ``properties.twilio_number`` UNIQUE constraint), which
    must still page (``alert_purchased_but_unrecorded``) and 502 normally.

    **Safety re-review finding 2 (#203):** the ``str(exc)`` substring
    fallback below is a client-influenced string (it can embed a
    landlord-supplied ``address_line1``, and the DB error's own ``DETAIL``
    text echoes submitted values verbatim) — a landlord who submits
    ``address_line1`` (or ``city``/``province``) equal to this constraint's
    own NAME could otherwise make an entirely UNRELATED integrity error
    (e.g. a ``NOT NULL`` violation on a different column, SQLSTATE
    ``23502``) misfire as a "duplicate address" 409 instead of the
    502/page an unrelated failure should get. Two independent gates close
    this:

    1. **The fallback is consulted ONLY when ``constraint_name`` could not
       be determined AT ALL** — never when it resolved to some OTHER,
       real, different constraint name. A resolved name is trusted
       unconditionally either way (matches → duplicate; doesn't match →
       NOT a duplicate, no fallback second-guessing).
    2. **The fallback also requires SQLSTATE ``23505`` (unique_violation)**
       — checked via ``pgcode``/``sqlstate`` on the underlying DBAPI
       exception (whichever the installed driver populates) — so a
       same-named substring appearing in a DIFFERENT class of integrity
       error (wrong SQLSTATE) can never be mistaken for this one.

    ``constraint_name`` itself is looked up on ``exc.orig`` first, then
    (some driver/SQLAlchemy-version combinations only populate it one
    level deeper) ``exc.orig.__cause__`` — the raw ``asyncpg`` exception
    SQLAlchemy's own DBAPI-compatibility wrapper chains from, empirically
    confirmed to carry ``constraint_name`` when the wrapper itself does
    not.
    """
    orig = getattr(exc, "orig", None)
    constraint_name = getattr(orig, "constraint_name", None)
    if constraint_name is None:
        constraint_name = getattr(getattr(orig, "__cause__", None), "constraint_name", None)

    if constraint_name is not None:
        return bool(constraint_name == _DUPLICATE_PROPERTY_CONSTRAINT_NAME)

    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if pgcode != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    return _DUPLICATE_PROPERTY_CONSTRAINT_NAME in str(exc)


def _row_to_property(row: RowMapping) -> PropertyResponse:
    return PropertyResponse(
        id=row["id"],
        label=row["label"],
        address_line1=row["address_line1"],
        city=row["city"],
        province=row["province"],
        postal_code=row["postal_code"],
        twilio_number=row["twilio_number"],
        house_rules=row["house_rules"],
        quiet_hours=row["quiet_hours"],
        heating_season=row["heating_season"],
        backup_contact=row["backup_contact"],
        open_case_count=int(row["open_case_count"]),
        created_at=row["created_at"],
    )


async def _get_property_or_404(
    session: AsyncSession, *, landlord_id: str, property_id: str
) -> RowMapping:
    row = (
        (
            await session.execute(
                _SELECT_ONE_SQL,
                {
                    "id": property_id,
                    "landlord_id": landlord_id,
                    "open_statuses": _OPEN_STATUSES_LIST,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(status_code=404, code="property_not_found", message="Property not found.")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/properties", response_model=PropertyListResponse)
async def list_properties(
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> PropertyListResponse:
    """List the caller's properties, newest-first, cursor-paginated."""
    landlord, session = landlord_and_session

    params: dict[str, Any] = {
        "landlord_id": str(landlord.id),
        "open_statuses": _OPEN_STATUSES_LIST,
        "limit_plus_one": limit + 1,
    }
    cursor_predicate = ""
    if cursor is not None:
        try:
            cursor_at, cursor_id = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise AppError(
                status_code=400, code="invalid_cursor", message="The cursor is invalid."
            ) from exc
        params["cursor_at"] = cursor_at
        params["cursor_id"] = cursor_id
        cursor_predicate = "AND (p.created_at, p.id) < (:cursor_at, CAST(:cursor_id AS uuid))"

    sql = text(
        f"SELECT {_PROPERTY_COLUMNS} FROM properties p "  # noqa: S608
        f"{_OPEN_CASE_COUNT_JOIN} "
        "WHERE p.landlord_id = :landlord_id "
        f"{cursor_predicate} "
        "ORDER BY p.created_at DESC, p.id DESC "
        "LIMIT :limit_plus_one"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    page, next_cursor = paginate_rows(rows, limit=limit, order_column="created_at")

    return PropertyListResponse(
        items=[_row_to_property(row) for row in page], next_cursor=next_cursor
    )


@router.post("/properties", response_model=PropertyResponse, status_code=201)
async def create_property(
    body: PropertyCreateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> PropertyResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    effective_province = body.province or "ON"

    # #53 safety review H1 — cheap pre-checks BEFORE any Twilio call (see
    # module docstring "Pre-Twilio-call money guards").
    existing_count = (
        await session.execute(_COUNT_LANDLORD_PROPERTIES_SQL, {"landlord_id": landlord_id})
    ).scalar_one()
    if existing_count >= settings.max_properties_per_landlord:
        raise AppError(
            status_code=409,
            code="property_limit_reached",
            message="You've reached the maximum number of properties.",
        )

    duplicate_row = (
        (
            await session.execute(
                _DUPLICATE_PROPERTY_SQL,
                {
                    "landlord_id": landlord_id,
                    "address_line1": body.address_line1,
                    "city": body.city,
                    "province": effective_province,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if duplicate_row is not None:
        raise AppError(
            status_code=409,
            code="duplicate_property",
            message="You already have a property at this address.",
        )

    try:
        provision_result = await property_provisioning.provision_number(
            area_code=body.area_code, province=effective_province
        )
    except property_provisioning.PublicBaseUrlUnconfiguredError as exc:
        raise AppError(
            status_code=500,
            code="public_base_url_unconfigured",
            message="Server is not configured for phone provisioning yet.",
        ) from exc
    except property_provisioning.NoNumbersAvailableError as exc:
        raise AppError(
            status_code=503,
            code="no_numbers_available",
            message="No phone numbers are available for that area right now.",
        ) from exc
    except property_provisioning.ProvisioningFailedError as exc:
        raise AppError(
            status_code=502,
            code="provisioning_failed",
            message="Could not provision a phone number right now.",
        ) from exc

    try:
        result = await session.execute(
            _INSERT_SQL,
            {
                "landlord_id": landlord_id,
                "label": body.label,
                "address_line1": body.address_line1,
                "city": body.city,
                "province": body.province,
                "postal_code": body.postal_code,
                "house_rules": body.house_rules,
                "backup_contact": json.dumps(body.backup_contact)
                if body.backup_contact is not None
                else None,
                "twilio_number": provision_result.phone_number,
                "twilio_sid": provision_result.twilio_sid,
            },
        )
        new_id = result.scalar_one()
        # Read-back is INSIDE this try too (safety review finding M3) — a
        # purchased number must never go silently unrecorded even if this
        # structurally-shouldn't-fail lookup somehow does.
        row = await _get_property_or_404(session, landlord_id=landlord_id, property_id=str(new_id))
        # #203's orphan-fix (shipped alongside item 2, NOT itself a
        # numbered #203 item — #203 item 1 is the deferred durable
        # -intent-row structural fix this partially substitutes for) —
        # commit explicitly HERE, inside this guarded try, rather than
        # leaving the actual COMMIT to require_landlord's/get_session's
        # post-handler teardown (app/db/session.py). Safe specifically
        # because nothing below this line touches `session` again (only
        # `log.info` + building the response model) — the mid-handler
        # -commit hazard require_landlord's own docstring warns about ("a
        # subsequent query on this session runs unscoped, fails closed to
        # zero rows") cannot occur here. This closes the #204 senior
        # -review finding: previously, a require_landlord teardown-commit
        # failure after a successful purchase ran entirely OUTSIDE this
        # try/except, so neither alert_purchased_but_unrecorded nor
        # release_number_best_effort ever ran for it — a purchased number
        # could be silently orphaned with zero signal. Folding the commit in
        # here means that failure now hits the SAME except clauses below as
        # every other post-purchase DB failure. get_session's own teardown
        # commit becomes a no-op logical commit after this succeeds
        # (SQLAlchemy's Session.commit() begins-and-commits an empty
        # transaction when there is nothing pending) — never a second real
        # round trip that could itself fail silently.
        #
        # AMBIGUOUS-COMMIT EDGE (safety re-review finding 1): this commit
        # can raise even though it durably succeeded (a lost ack, not a
        # real failure). release_number_best_effort's own L2 guard
        # (app/property_provisioning.py, mirroring the deprovisioning
        # sweep's "never release a SID a live property references") is
        # what makes the except branches below safe even in that case --
        # never silencing a property's tenant-facing/emergency line by
        # releasing a number a LIVE row still owns.
        await session.commit()
    except IntegrityError as exc:
        if _is_duplicate_property_unique_violation(exc):
            # #203 item 2 — the loser of a genuine concurrent-create race:
            # migration 0013's unique index caught what the pre-check SELECT
            # (TOCTOU) couldn't. Release the just-purchased number through
            # the EXISTING compensation seam (no new Twilio call site) and
            # return the ordinary duplicate_property 409 — no Sentry page,
            # since the schema itself resolving a race is expected,
            # self-healing behavior, not a bug.
            await property_provisioning.release_number_best_effort(provision_result.twilio_sid)
            raise AppError(
                status_code=409,
                code="duplicate_property",
                message="You already have a property at this address.",
            ) from exc
        # Any OTHER integrity error after a successful purchase (or the
        # explicit commit above failing) -- same compensation as the
        # generic Exception branch below. ALWAYS pages (M3) regardless of
        # whether the compensating release itself succeeds --
        # release_number_best_effort has its own, separate alert for a
        # release that itself fails.
        property_provisioning.alert_purchased_but_unrecorded(provision_result.twilio_sid)
        await property_provisioning.release_number_best_effort(provision_result.twilio_sid)
        raise AppError(
            status_code=502,
            code="provisioning_failed",
            message="Could not provision a phone number right now.",
        ) from exc
    except Exception as exc:
        # DB write (or its read-back, or the explicit commit above) failed
        # AFTER a successful purchase -- compensate rather than leave a
        # purchased-but-orphaned number (never-break: no half-provisioned
        # row, and no unreferenced live Twilio number either). See
        # property_provisioning.py's module docstring. ALWAYS pages (M3)
        # regardless of whether the compensating release itself succeeds --
        # release_number_best_effort has its own, separate alert for a
        # release that itself fails.
        property_provisioning.alert_purchased_but_unrecorded(provision_result.twilio_sid)
        await property_provisioning.release_number_best_effort(provision_result.twilio_sid)
        raise AppError(
            status_code=502,
            code="provisioning_failed",
            message="Could not provision a phone number right now.",
        ) from exc

    log.info(
        "property_provisioned",
        landlord_id=landlord_id,
        property_id=str(new_id),
        twilio_sid=provision_result.twilio_sid,
        a2p_status=provision_result.a2p_status,
    )
    return _row_to_property(row)


@router.get("/properties/{property_id}", response_model=PropertyResponse)
async def get_property(
    property_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> PropertyResponse:
    landlord, session = landlord_and_session
    row = await _get_property_or_404(
        session, landlord_id=str(landlord.id), property_id=str(property_id)
    )
    return _row_to_property(row)


@router.patch("/properties/{property_id}", response_model=PropertyResponse)
async def update_property(
    property_id: UUID,
    body: PropertyUpdateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> PropertyResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    prop_id = str(property_id)

    # Existence check first — 404 before any write, and gives us the
    # pre-update house_rules value for the audit-log diff below.
    existing = await _get_property_or_404(session, landlord_id=landlord_id, property_id=prop_id)

    provided = body.model_dump(exclude_unset=True)
    if not provided:
        return _row_to_property(existing)

    # label/address_line1/city/province/quiet_hours/heating_season are all
    # NOT NULL in schema-v1.md — reject an explicit null for any of them
    # rather than attempting a write the DB would bounce as an
    # IntegrityError (senior review on PR #195, B3; postal_code/
    # house_rules/backup_contact are genuinely nullable, so absent below).
    reject_explicit_null(
        provided,
        not_nullable_fields=[
            "label",
            "address_line1",
            "city",
            "province",
            "quiet_hours",
            "heating_season",
        ],
    )

    set_clauses: list[str] = []
    params: dict[str, Any] = {"id": prop_id, "landlord_id": landlord_id}
    jsonb_fields = {"backup_contact", "quiet_hours", "heating_season"}
    for field, value in provided.items():
        set_clauses.append(
            f"{field} = CAST(:{field} AS jsonb)" if field in jsonb_fields else f"{field} = :{field}"
        )
        params[field] = json.dumps(value) if field in jsonb_fields else value
    set_clauses.append("updated_at = now()")

    update_sql = text(
        "UPDATE properties SET "  # noqa: S608
        + ", ".join(set_clauses)
        + " WHERE id = :id AND landlord_id = :landlord_id"
    )
    await session.execute(update_sql, params)

    updated = await _get_property_or_404(session, landlord_id=landlord_id, property_id=prop_id)

    if "house_rules" in provided and updated["house_rules"] != existing["house_rules"]:
        await record_audit_log(
            session,
            landlord_id=landlord_id,
            actor="landlord",
            action="settings_changed",
            payload={"resource": "property", "property_id": prop_id, "field": "house_rules"},
        )

    return _row_to_property(updated)


@router.delete("/properties/{property_id}", status_code=204)
async def delete_property(
    property_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    confirm: bool = False,
) -> None:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    prop_id = str(property_id)

    existing = await _get_property_or_404(session, landlord_id=landlord_id, property_id=prop_id)

    # #53: deleting a property with a live phone number is irreversible for
    # that number (deprovisioning below only delays the release, it never
    # undoes this delete) — require an explicit ?confirm=true before doing
    # anything else. Checked before the open-cases/dependents business
    # checks below (api-contracts.md's v1.12 amendment).
    if not confirm:
        raise AppError(
            status_code=400,
            code="confirmation_required",
            message="Pass ?confirm=true to delete this property.",
        )

    open_count = (
        await session.execute(
            _COUNT_OPEN_CASES_SQL,
            {"id": prop_id, "landlord_id": landlord_id, "open_statuses": _OPEN_STATUSES_LIST},
        )
    ).scalar_one()
    if open_count > 0:
        raise AppError(
            status_code=409,
            code="has_open_cases",
            message="This property has open cases and cannot be deleted.",
        )

    try:
        await session.execute(_DELETE_SQL, {"id": prop_id, "landlord_id": landlord_id})
    except IntegrityError as exc:
        # FK RESTRICT from tenants/cases/messages — proposed contract
        # addition, see module docstring.
        raise AppError(
            status_code=409,
            code="has_dependents",
            message="This property has related records and cannot be deleted.",
        ) from exc

    twilio_sid = existing["twilio_sid"]
    if twilio_sid:
        # Deprovisioning (#53): the property row is already gone (hard
        # delete, unchanged) — schedule the actual Twilio release for
        # after the grace period rather than calling Twilio synchronously
        # here. See property_provisioning.py's module docstring.
        await property_provisioning.schedule_number_release(
            session, landlord_id=landlord_id, property_id=prop_id, twilio_sid=twilio_sid
        )
