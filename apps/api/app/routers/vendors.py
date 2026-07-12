"""Vendors CRUD (#54 — bundled with Properties per the sub-resource shape
in ``docs/03-engineering/api-contracts.md``'s "Tenants & Vendors" section:
``GET/POST /v1/vendors`` · ``PATCH/DELETE /v1/vendors/{id}``).

Landlord-scoped via ``Depends(require_landlord)`` — see
``app/routers/properties.py``'s module docstring for the RLS +
explicit-``landlord_id`` rationale.

``DELETE`` is a SOFT delete: ``vendors`` has an ``active`` flag but no
``deleted_at`` column (schema-v1.md) — same rationale as
``app/routers/tenants.py``. No FK targeting ``vendors(id)`` is an explicit
``ON DELETE RESTRICT``; ``cases.vendor_id``/``messages.vendor_id`` carry
no explicit ``ON DELETE`` clause (Postgres default ``NO ACTION``), which
still blocks an immediate hard delete while a referencing row exists.
Sets ``active = false``; idempotent.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Landlord, require_landlord
from app.errors import AppError
from app.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursorError,
    decode_cursor,
    paginate_rows,
)

router = APIRouter(prefix="/v1", tags=["vendors"])

Trade = Literal[
    "plumbing", "electrical", "hvac", "appliance", "locksmith", "pest", "general", "other"
]

# ---------------------------------------------------------------------------
# Response / request models — schema-v1.md's `vendors` table, verbatim.
# ---------------------------------------------------------------------------


class VendorResponse(BaseModel):
    id: UUID
    name: str
    trade: Trade
    phone: str
    notes: str | None
    working_hours: dict[str, Any] | None
    active: bool
    created_at: datetime


class VendorListResponse(BaseModel):
    items: list[VendorResponse]
    next_cursor: str | None


class VendorCreateRequest(BaseModel):
    name: str
    trade: Trade
    phone: str
    notes: str | None = None
    working_hours: dict[str, Any] | None = None
    active: bool = True


class VendorUpdateRequest(BaseModel):
    name: str | None = None
    trade: Trade | None = None
    phone: str | None = None
    notes: str | None = None
    working_hours: dict[str, Any] | None = None
    active: bool | None = None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_VENDOR_COLUMNS = "id, name, trade, phone, notes, working_hours, active, created_at"

_SELECT_ONE_SQL = text(
    f"SELECT {_VENDOR_COLUMNS} FROM vendors WHERE id = :id AND landlord_id = :landlord_id"  # noqa: S608
)

_INSERT_SQL = text(
    """
    INSERT INTO vendors (landlord_id, name, trade, phone, notes, working_hours, active)
    VALUES (:landlord_id, :name, :trade, :phone, :notes, CAST(:working_hours AS jsonb), :active)
    RETURNING id
    """
)

_DEACTIVATE_SQL = text(
    "UPDATE vendors SET active = false, updated_at = now() "
    "WHERE id = :id AND landlord_id = :landlord_id"
)


def _row_to_vendor(row: RowMapping) -> VendorResponse:
    return VendorResponse(
        id=row["id"],
        name=row["name"],
        trade=row["trade"],
        phone=row["phone"],
        notes=row["notes"],
        working_hours=row["working_hours"],
        active=row["active"],
        created_at=row["created_at"],
    )


async def _get_vendor_or_404(
    session: AsyncSession, *, landlord_id: str, vendor_id: str
) -> RowMapping:
    row = (
        (await session.execute(_SELECT_ONE_SQL, {"id": vendor_id, "landlord_id": landlord_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(status_code=404, code="vendor_not_found", message="Vendor not found.")
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/vendors", response_model=VendorListResponse)
async def list_vendors(
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> VendorListResponse:
    landlord, session = landlord_and_session

    params: dict[str, Any] = {"landlord_id": str(landlord.id), "limit_plus_one": limit + 1}
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
        cursor_predicate = "AND (created_at, id) < (:cursor_at, CAST(:cursor_id AS uuid))"

    sql = text(
        f"SELECT {_VENDOR_COLUMNS} FROM vendors "  # noqa: S608
        "WHERE landlord_id = :landlord_id "
        f"{cursor_predicate} "
        "ORDER BY created_at DESC, id DESC "
        "LIMIT :limit_plus_one"
    )
    rows = (await session.execute(sql, params)).mappings().all()
    page, next_cursor = paginate_rows(rows, limit=limit, order_column="created_at")

    return VendorListResponse(items=[_row_to_vendor(row) for row in page], next_cursor=next_cursor)


@router.post("/vendors", response_model=VendorResponse, status_code=201)
async def create_vendor(
    body: VendorCreateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> VendorResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)

    result = await session.execute(
        _INSERT_SQL,
        {
            "landlord_id": landlord_id,
            "name": body.name,
            "trade": body.trade,
            "phone": body.phone,
            "notes": body.notes,
            "working_hours": None if body.working_hours is None else _dump_json(body.working_hours),
            "active": body.active,
        },
    )
    new_id = result.scalar_one()
    row = await _get_vendor_or_404(session, landlord_id=landlord_id, vendor_id=str(new_id))
    return _row_to_vendor(row)


@router.patch("/vendors/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: UUID,
    body: VendorUpdateRequest,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> VendorResponse:
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    vid = str(vendor_id)

    await _get_vendor_or_404(session, landlord_id=landlord_id, vendor_id=vid)

    provided = body.model_dump(exclude_unset=True)
    if provided:
        set_clauses: list[str] = []
        params: dict[str, Any] = {"id": vid, "landlord_id": landlord_id}
        for field, value in provided.items():
            if field == "working_hours":
                set_clauses.append("working_hours = CAST(:working_hours AS jsonb)")
                params["working_hours"] = None if value is None else _dump_json(value)
            else:
                set_clauses.append(f"{field} = :{field}")
                params[field] = value
        set_clauses.append("updated_at = now()")
        update_sql = text(
            "UPDATE vendors SET "  # noqa: S608
            + ", ".join(set_clauses)
            + " WHERE id = :id AND landlord_id = :landlord_id"
        )
        await session.execute(update_sql, params)

    row = await _get_vendor_or_404(session, landlord_id=landlord_id, vendor_id=vid)
    return _row_to_vendor(row)


@router.delete("/vendors/{vendor_id}", response_model=VendorResponse)
async def delete_vendor(
    vendor_id: UUID,
    landlord_and_session: Annotated[tuple[Landlord, AsyncSession], Depends(require_landlord)],
) -> VendorResponse:
    """Soft-delete: sets ``active = false`` (see module docstring). Idempotent."""
    landlord, session = landlord_and_session
    landlord_id = str(landlord.id)
    vid = str(vendor_id)

    await _get_vendor_or_404(session, landlord_id=landlord_id, vendor_id=vid)
    await session.execute(_DEACTIVATE_SQL, {"id": vid, "landlord_id": landlord_id})

    row = await _get_vendor_or_404(session, landlord_id=landlord_id, vendor_id=vid)
    return _row_to_vendor(row)


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value)
