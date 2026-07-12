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

Twilio provisioning (the doc's "Provisions a Twilio number (#53)" note) is
issue #53's job, not this one's — #53 is a separate, not-yet-implemented
issue, and ``app/integrations/twilio*.py`` is out of scope for this PR.
``POST /v1/properties`` here creates the row with ``twilio_number`` left
``NULL`` (schema-v1.md: "E.164; null until provisioned" — the column is
already nullable for exactly this reason).

``DELETE`` is a genuine hard delete (``properties`` has no ``deleted_at``
column, unlike ``tenants``/``vendors``' ``active`` flag) — the documented
``has_open_cases`` 409 is the first-line business check. A property that
survives that check but still has FK-referencing ``tenants``/``cases``/
``messages`` rows (``ON DELETE RESTRICT``) is caught as a second-line
``IntegrityError`` and surfaced as a clean 409 ``has_dependents`` rather
than a raw 500 — a contract addition proposed in the same PR (see
``api-contracts.md``'s Properties section, "DELETE" note).

Audit trail (#54 AC: "audit entries on changes that affect agent
behavior"): a ``PATCH`` that actually changes ``house_rules`` writes an
``audit_log`` row (``actor='landlord'``, ``action='settings_changed'``) —
compared against the pre-update value so a no-op PATCH (same value resent)
never writes a spurious entry.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import OPEN_STATUSES
from app.audit import record_audit_log
from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursorError,
    decode_cursor,
    paginate_rows,
)

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
    label: str
    address_line1: str
    city: str
    province: str | None = None
    postal_code: str | None = None
    house_rules: str | None = None
    backup_contact: dict[str, Any] | None = None


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

_PROPERTY_COLUMNS = (
    "p.id, p.label, p.address_line1, p.city, p.province, p.postal_code, "
    "p.twilio_number, p.house_rules, p.quiet_hours, p.heating_season, "
    "p.backup_contact, p.created_at, COALESCE(oc.open_case_count, 0) AS open_case_count"
)

_OPEN_CASE_COUNT_JOIN = (
    "LEFT JOIN ("
    "SELECT property_id, COUNT(*) AS open_case_count FROM cases "
    "WHERE status = ANY(:open_statuses) GROUP BY property_id"
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
                             postal_code, house_rules, backup_contact)
    VALUES (:landlord_id, :label, :address_line1, :city, COALESCE(:province, 'ON'),
            :postal_code, :house_rules, CAST(:backup_contact AS jsonb))
    RETURNING id
    """
)

_COUNT_OPEN_CASES_SQL = text(
    "SELECT COUNT(*) FROM cases WHERE property_id = :id AND status = ANY(:open_statuses)"
)

_DELETE_SQL = text("DELETE FROM properties WHERE id = :id AND landlord_id = :landlord_id")


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

    result = await session.execute(
        _INSERT_SQL,
        {
            "landlord_id": str(landlord.id),
            "label": body.label,
            "address_line1": body.address_line1,
            "city": body.city,
            "province": body.province,
            "postal_code": body.postal_code,
            "house_rules": body.house_rules,
            "backup_contact": json.dumps(body.backup_contact)
            if body.backup_contact is not None
            else None,
        },
    )
    new_id = result.scalar_one()

    row = await _get_property_or_404(session, landlord_id=str(landlord.id), property_id=str(new_id))
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

    # quiet_hours/heating_season are NOT NULL in schema-v1.md — reject an
    # explicit null rather than attempting a write that the DB would bounce
    # as an IntegrityError.
    for not_nullable_field in ("quiet_hours", "heating_season"):
        if not_nullable_field in provided and provided[not_nullable_field] is None:
            raise AppError(
                status_code=422,
                code="invalid_field",
                message=f"{not_nullable_field} cannot be null.",
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
) -> None:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    prop_id = str(property_id)

    await _get_property_or_404(session, landlord_id=landlord_id, property_id=prop_id)

    open_count = (
        await session.execute(
            _COUNT_OPEN_CASES_SQL, {"id": prop_id, "open_statuses": _OPEN_STATUSES_LIST}
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
