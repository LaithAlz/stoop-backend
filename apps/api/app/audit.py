"""Shared ``audit_log`` INSERT helper.

``audit_log`` is append-only (never-break rule #2) — this module only ever
INSERTs, never UPDATEs/DELETEs, matching the REVOKE migration 0005 already
enforces at the grant level.

Used by the read/write-adjacent routers added by issues #54/#57
(``app/routers/properties.py``, ``app/routers/me.py``) for the
``'settings_changed'`` audit entries their acceptance criteria require on
changes that affect agent behavior (``house_rules``, ``voice_profile``).

Callers MUST NOT ``await session.commit()`` after calling this — every
request-path session dependency in ``app/db/session.py`` commits once at
teardown; a mid-handler commit ends ``require_landlord``'s ``SET LOCAL``
GUC early (``app/deps.py``'s documented trap).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT_AUDIT_LOG_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, :actor, :action, CAST(:payload AS jsonb))"
)


async def record_audit_log(
    session: AsyncSession,
    *,
    landlord_id: str,
    actor: str,
    action: str,
    case_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one ``audit_log`` row.

    ``actor``/``action`` must be values from ``schema-v1.md``'s CHECK
    vocabulary — never invented here. ``payload`` should carry structural
    facts only (e.g. which field changed), never tenant phone numbers or
    message bodies (never-break rule #5) — house_rules/voice_profile are
    landlord-authored settings, not tenant PII, but callers still keep
    payloads minimal by convention.
    """
    await session.execute(
        _INSERT_AUDIT_LOG_SQL,
        {
            "landlord_id": landlord_id,
            "case_id": case_id,
            "actor": actor,
            "action": action,
            "payload": json.dumps(payload or {}),
        },
    )


__all__: list[str] = ["record_audit_log"]
