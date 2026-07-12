"""Tenants CRUD (#54 — bundled with Properties per the sub-resource shape
in ``docs/03-engineering/api-contracts.md``'s "Tenants & Vendors" section:
``GET/POST /v1/properties/{id}/tenants`` · ``PATCH/DELETE /v1/tenants/{id}``).

Landlord-scoped via ``Depends(require_landlord)`` — RLS GUC plus an
explicit ``landlord_id`` predicate on every query (belt-and-braces, see
``app/routers/properties.py``'s module docstring for the same rationale).

``DELETE`` is a SOFT delete: ``tenants`` has an ``active`` flag but no
``deleted_at`` column (schema-v1.md), and ``cases``/``messages`` reference
``tenants.id`` with ``ON DELETE RESTRICT`` — a tenant with any case/message
history can never be hard-deleted anyway. ``DELETE /v1/tenants/{id}`` sets
``active = false`` and returns the updated row; idempotent (deleting an
already-inactive tenant just re-confirms the state, no error).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Landlord, require_landlord
from app.errors import AppError

router = APIRouter(prefix="/v1", tags=["tenants"])

VulnerableOccupant = Literal["infant", "elderly", "medical_device"]

# ---------------------------------------------------------------------------
# Response / request models — schema-v1.md's `tenants` table, verbatim.
# ---------------------------------------------------------------------------


class TenantResponse(BaseModel):
    id: UUID
    property_id: UUID
    name: str | None
    phone: str
    unit: str | None
    vulnerable_occupant: VulnerableOccupant | None
    notes: str | None
    active: bool
    created_at: datetime


class TenantListResponse(BaseModel):
    items: list[TenantResponse]
    next_cursor: str | None


class TenantCreateRequest(BaseModel):
    phone: str
    name: str | None = None
    unit: str | None = None
    vulnerable_occupant: VulnerableOccupant | None = None
    notes: str | None = None


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    phone: str | None = None
    unit: str | None = None
    vulnerable_occupant: VulnerableOccupant | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_TENANT_COLUMNS = (
    "id, property_id, name, phone, unit, vulnerable_occupant, notes, active, created_at"
)

_SELECT_PROPERTY_SQL = text(
    "SELECT id FROM properties WHERE id = :id AND landlord_id = :landlord_id"
)

_SELECT_TENANTS_FOR_PROPERTY_SQL = text(
    f"SELECT {_TENANT_COLUMNS} FROM tenants "  # noqa: S608
    "WHERE property_id = :property_id AND landlord_id = :landlord_id "
    "ORDER BY created_at DESC, id DESC"
)

_SELECT_ONE_SQL = text(
    f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = :id AND landlord_id = :landlord_id"  # noqa: S608
)

_INSERT_SQL = text(
    """
    INSERT INTO tenants (landlord_id, property_id, name, phone, unit, vulnerable_occupant, notes)
    VALUES (:landlord_id, :property_id, :name, :phone, :unit, :vulnerable_occupant, :notes)
    RETURNING id
    """
)

_DEACTIVATE_SQL = text(
    "UPDATE tenants SET active = false, updated_at = now() "
    "WHERE id = :id AND landlord_id = :landlord_id"
)


def _row_to_tenant(row: RowMapping) -> TenantResponse:
    return TenantResponse(
        id=row["id"],
        property_id=row["property_id"],
        name=row["name"],
        phone=row["phone"],
        unit=row["unit"],
        vulnerable_occupant=row["vulnerable_occupant"],
        notes=row["notes"],
        active=row["active"],
        created_at=row["created_at"],
    )


async def _get_property_or_404(
    session: AsyncSession, *, landlord_id: str, property_id: str
) -> None:
    row = (
        await session.execute(_SELECT_PROPERTY_SQL, {"id": property_id, "landlord_id": landlord_id})
    ).one_or_none()
    if row is None:
        raise AppError(status_code=404, code="property_not_found", message="Property not found.")


async def _get_tenant_or_404(
    session: AsyncSession, *, landlord_id: str, tenant_id: str
) -> RowMapping:
    row = (
        (await session.execute(_SELECT_ONE_SQL, {"id": tenant_id, "landlord_id": landlord_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(status_code=404, code="tenant_not_found", message="Tenant not found.")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/properties/{property_id}/tenants", response_model=TenantListResponse)
async def list_tenants(
    property_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> TenantListResponse:
    """List tenants for one property. Not cursor-paginated (per-property
    tenant counts are small; consistent with the un-paginated list shapes
    already accepted elsewhere, e.g. ``GET /v1/properties`` in practice)."""
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)

    await _get_property_or_404(session, landlord_id=landlord_id, property_id=str(property_id))

    rows = (
        (
            await session.execute(
                _SELECT_TENANTS_FOR_PROPERTY_SQL,
                {"property_id": str(property_id), "landlord_id": landlord_id},
            )
        )
        .mappings()
        .all()
    )
    return TenantListResponse(items=[_row_to_tenant(row) for row in rows], next_cursor=None)


@router.post("/properties/{property_id}/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(
    property_id: UUID,
    body: TenantCreateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> TenantResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)

    await _get_property_or_404(session, landlord_id=landlord_id, property_id=str(property_id))

    result = await session.execute(
        _INSERT_SQL,
        {
            "landlord_id": landlord_id,
            "property_id": str(property_id),
            "name": body.name,
            "phone": body.phone,
            "unit": body.unit,
            "vulnerable_occupant": body.vulnerable_occupant,
            "notes": body.notes,
        },
    )
    new_id = result.scalar_one()
    row = await _get_tenant_or_404(session, landlord_id=landlord_id, tenant_id=str(new_id))
    return _row_to_tenant(row)


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: UUID,
    body: TenantUpdateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> TenantResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    tid = str(tenant_id)

    await _get_tenant_or_404(session, landlord_id=landlord_id, tenant_id=tid)

    provided = body.model_dump(exclude_unset=True)
    if provided:
        set_clauses = [f"{field} = :{field}" for field in provided]
        set_clauses.append("updated_at = now()")
        params: dict[str, object] = {"id": tid, "landlord_id": landlord_id, **provided}
        update_sql = text(
            "UPDATE tenants SET "  # noqa: S608
            + ", ".join(set_clauses)
            + " WHERE id = :id AND landlord_id = :landlord_id"
        )
        await session.execute(update_sql, params)

    row = await _get_tenant_or_404(session, landlord_id=landlord_id, tenant_id=tid)
    return _row_to_tenant(row)


@router.delete("/tenants/{tenant_id}", response_model=TenantResponse)
async def delete_tenant(
    tenant_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> TenantResponse:
    """Soft-delete: sets ``active = false`` (see module docstring). Idempotent."""
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    tid = str(tenant_id)

    await _get_tenant_or_404(session, landlord_id=landlord_id, tenant_id=tid)
    await session.execute(_DEACTIVATE_SQL, {"id": tid, "landlord_id": landlord_id})

    row = await _get_tenant_or_404(session, landlord_id=landlord_id, tenant_id=tid)
    return _row_to_tenant(row)
