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

``normalize_blank_to_null`` (#260) is a small sibling: some not-nullable
-by-business-rule fields (today: ``phone`` — the emergency-call target,
schema-v1.md) must reject an explicit empty string exactly like an
explicit ``null``, since an empty string clears the column just as
effectively. Callers opt a field in explicitly (never all fields
implicitly) and run this BEFORE ``reject_explicit_null`` so the empty
-string case collapses onto the exact same code/message path as the null
case, rather than needing a second, parallel error shape.
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


def normalize_blank_to_null(provided: dict[str, Any], *, fields: list[str]) -> None:
    """Mutate *provided* in place: for each name in *fields*, an explicit
    empty (or whitespace-only) string value becomes ``None`` (#260).

    Intended to run immediately before ``reject_explicit_null`` with the
    SAME field included in that call's ``not_nullable_fields`` — the two
    together make ``""`` 422 with the exact same code and message an
    explicit ``null`` already gets, rather than silently writing a blank
    string to a column whose business rule says it must never be cleared
    by accident (e.g. ``landlords.phone``, the emergency-call target).
    Only touches fields explicitly listed — never a blanket string-empties
    -to-null pass over the whole body, which would wrongly affect genuinely
    nullable free-text fields (``notes``, ``house_rules``, …).
    """
    for field in fields:
        value = provided.get(field)
        if field in provided and isinstance(value, str) and value.strip() == "":
            provided[field] = None


__all__: list[str] = ["normalize_blank_to_null", "reject_explicit_null"]
