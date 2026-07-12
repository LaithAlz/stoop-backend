"""Shared PATCH-body validation helper.

Every ``*UpdateRequest`` model in this codebase types its optional fields
as ``X | None = None`` so ``exclude_unset`` can tell "field omitted" from
"field explicitly provided" — but that same shape lets a client send an
explicit JSON ``null`` for a column that is ``NOT NULL`` in schema-v1.md.
Without a guard, that reaches the database as a bound ``NULL`` parameter
and raises an uncaught ``IntegrityError`` (``NotNullViolation``) — a raw
500, not the standard error envelope (senior review on PR #195, B3).

``reject_explicit_null`` is the one place this check lives; every router's
``update_*`` handler calls it, right after ``model_dump(exclude_unset=True)``
and before building any SQL, so the rejection is always fail-closed (no
write attempted) and always the same code/message shape.
"""

from __future__ import annotations

from typing import Any

from app.errors import AppError


def reject_explicit_null(provided: dict[str, Any], *, not_nullable_fields: list[str]) -> None:
    """Raise 422 ``invalid_field`` if any of ``not_nullable_fields`` is
    present in ``provided`` with a value of ``None``.

    ``provided`` is the result of ``body.model_dump(exclude_unset=True)`` —
    a field only appears here if the client actually sent it, so this never
    fires for a field the client simply omitted.
    """
    for field in not_nullable_fields:
        if field in provided and provided[field] is None:
            raise AppError(
                status_code=422,
                code="invalid_field",
                message=f"{field} cannot be null.",
            )


__all__: list[str] = ["reject_explicit_null"]
