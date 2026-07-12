"""Opaque cursor pagination — shared helper for every ``/v1/`` list endpoint.

Per ``docs/03-engineering/api-contracts.md``'s pagination convention:
``?limit=`` (default 25, max 100) + ``?cursor=``; responses carry
``"next_cursor": string|null``. Lists are newest-first — the one documented
exception is ``GET /v1/queue`` (deliberately unpaginated, oldest-first per
severity tier), which is out of scope for the endpoints in this module
(``docs/03-engineering/api-contracts.md``'s "Ordering/pagination" note under
Queue v1.1 amendments).

Keyset (seek) pagination on ``(order_column, id)`` rather than ``OFFSET``:
stable under concurrent inserts — an ``OFFSET``-based page can skip or
duplicate rows as new rows land between page fetches; a keyset cursor
always resumes strictly after the last-seen ``(order_column, id)`` pair, so
it cannot.

The cursor is a black box to callers by design (never documented as parsed
JSON, never relied on by any client) — ``encode_cursor``/``decode_cursor``
are the only code allowed to construct or interpret one.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import RowMapping

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class InvalidCursorError(Exception):
    """Raised when a client-supplied cursor cannot be decoded.

    Callers should catch this and respond with the standard error envelope
    (``AppError``, 400 ``invalid_cursor``) — never let it propagate to a
    bare 500.
    """


def encode_cursor(order_key: datetime, row_id: str) -> str:
    """Encode a keyset pagination cursor as an opaque, url-safe string."""
    payload = json.dumps({"k": order_key.isoformat(), "id": str(row_id)})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a cursor produced by :func:`encode_cursor`.

    Validates ``id`` is a well-formed uuid (every ``id`` column callers key
    a keyset query off is a uuid) — a crafted cursor with valid base64/JSON
    but a non-uuid ``id`` (e.g. ``"id": "'; drop--"``) would otherwise reach
    the caller's ``CAST(:cursor_id AS uuid)`` and raise a raw, uncaught
    ``DataError`` (500) instead of the standard error envelope (senior
    review on PR #195, B2).

    Raises
    ------
    InvalidCursorError
        The cursor is not valid base64/JSON, is missing the expected keys,
        or ``id`` is not a valid uuid.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        # NORMALIZE, don't just shape-validate (senior re-review on PR #195):
        # uuid.UUID accepts non-canonical forms ("urn:uuid:...", "{...}")
        # that asyncpg's bind encoder rejects as raw strings -> DBAPIError
        # 500. str(uuid.UUID(...)) canonicalizes. Likewise extreme-offset
        # ISO datetimes pass fromisoformat but under/overflow inside
        # asyncpg's UTC conversion -> astimezone(UTC) surfaces OverflowError
        # here, inside the except net.
        row_id = str(uuid.UUID(str(payload["id"])))
        order_key = datetime.fromisoformat(payload["k"]).astimezone(UTC)
        return order_key, row_id
    except (
        binascii.Error,
        ValueError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        OverflowError,
    ) as exc:
        raise InvalidCursorError("invalid cursor") from exc


def paginate_rows(
    rows: Sequence[RowMapping], *, limit: int, order_column: str
) -> tuple[list[RowMapping], str | None]:
    """Split a ``limit + 1``-row fetch into ``(page, next_cursor)``.

    Callers execute their keyset query with ``LIMIT :limit_plus_one`` (see
    module docstring) and pass the raw result here. Fetching one extra row
    is how we know whether a next page exists without a separate COUNT
    query; the extra row itself is dropped, never returned to the caller.
    """
    has_more = len(rows) > limit
    page = list(rows[:limit])
    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last[order_column], str(last["id"]))
    return page, next_cursor


__all__: list[str] = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "InvalidCursorError",
    "decode_cursor",
    "encode_cursor",
    "paginate_rows",
]
