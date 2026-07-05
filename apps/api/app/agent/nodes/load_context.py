"""``load_context`` node (#30) — loads everything the downstream
classify/draft nodes need: property + landlord + tenant context, the
tenant's open cases, recent channel history, and weather.

Runs after ``identify_property`` (reads ``state["case_context"]`` for
``property_id``/``tenant_id``/``landlord_id``, already populated there).

Channel-history window
-----------------------
conversation-model.md does not specify a size for "recent channel history";
this module defaults to ``_CHANNEL_HISTORY_LIMIT = 20`` messages (documented
here, the single place to tune it). Query is scoped to
``messages.tenant_id`` — this ALSO naturally excludes landlord command-
channel rows (``party='landlord'``, which always carry ``tenant_id IS
NULL`` per schema-v1.md) and vendor rows (``tenant_id IS NULL`` for those
too) without an extra filter, matching conversation-model.md's "channel"
definition exactly (the tenant<->property SMS relationship only).

Weather
-------
Delegates to ``app/integrations/weather.py``. A property with no
``lat``/``lon`` on file, or any provider failure/timeout, produces
``weather=None`` plus a "weather unavailable" reasoning_log note —
severity-rubric-v1.md's bias rule (escalate when uncertain) is what covers
this gap downstream; this node never blocks or raises for it.

DB access
---------
Admin engine (background/graph context) — same pattern as
``identify_property``. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import OPEN_STATUSES
from app.agent.schemas import CaseContext, ChannelMessage, OpenCaseSummary, VulnerableOccupant
from app.agent.state import AgentState
from app.db.session import get_admin_session
from app.integrations.weather import get_weather_snapshot

log = structlog.get_logger(__name__)

_CHANNEL_HISTORY_LIMIT = 20
"""Default window size for the recent-channel-history slice — see module
docstring. Not specified by conversation-model.md; picked as a reasonable
default and documented here for anyone who wants to tune it."""

_OPEN_STATUS_LITERAL_LIST = ", ".join(f"'{status}'" for status in sorted(OPEN_STATUSES))

_SELECT_PROPERTY_SQL = text(
    "SELECT house_rules, quiet_hours, heating_season, backup_contact, lat, lon "
    "FROM properties WHERE id = :property_id"
)

_SELECT_LANDLORD_VOICE_PROFILE_SQL = text(
    "SELECT voice_profile FROM landlords WHERE id = :landlord_id"
)

_SELECT_TENANT_SQL = text("SELECT vulnerable_occupant FROM tenants WHERE id = :tenant_id")

_SELECT_OPEN_CASES_SQL = text(
    "SELECT id, status, severity, intent, title, last_activity_at FROM cases "  # noqa: S608
    f"WHERE tenant_id = :tenant_id AND status IN ({_OPEN_STATUS_LITERAL_LIST}) "
    "ORDER BY last_activity_at DESC"
    # ^ IN-list built from the internal OPEN_STATUSES constant only, never
    #   external/request-supplied data — not a real injection vector.
)

_SELECT_CHANNEL_HISTORY_SQL = text(
    "SELECT direction, body, created_at FROM messages "
    "WHERE tenant_id = :tenant_id ORDER BY created_at DESC LIMIT :limit"
)


async def _load_property(session: AsyncSession, property_id: UUID) -> dict[str, Any]:
    row = (
        (await session.execute(_SELECT_PROPERTY_SQL, {"property_id": str(property_id)}))
        .mappings()
        .one()
    )
    return dict(row)


async def _load_voice_profile(session: AsyncSession, landlord_id: UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                _SELECT_LANDLORD_VOICE_PROFILE_SQL, {"landlord_id": str(landlord_id)}
            )
        )
        .mappings()
        .one()
    )
    voice_profile: dict[str, Any] | None = row["voice_profile"]
    return voice_profile


async def _load_vulnerable_occupant(session: AsyncSession, tenant_id: UUID) -> str | None:
    row = (
        (await session.execute(_SELECT_TENANT_SQL, {"tenant_id": str(tenant_id)})).mappings().one()
    )
    vulnerable_occupant: str | None = row["vulnerable_occupant"]
    return vulnerable_occupant


async def _load_open_cases(session: AsyncSession, tenant_id: UUID) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(_SELECT_OPEN_CASES_SQL, {"tenant_id": str(tenant_id)}))
        .mappings()
        .all()
    )
    return [
        OpenCaseSummary(
            case_id=row["id"],
            status=row["status"],
            severity=row["severity"],
            intent=row["intent"],
            title=row["title"],
            last_activity_at=row["last_activity_at"].isoformat(),
        ).model_dump(mode="json")
        for row in rows
    ]


async def _load_channel_history(session: AsyncSession, tenant_id: UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                _SELECT_CHANNEL_HISTORY_SQL,
                {"tenant_id": str(tenant_id), "limit": _CHANNEL_HISTORY_LIMIT},
            )
        )
        .mappings()
        .all()
    )
    # Query returns most-recent-first (for the LIMIT to keep the RIGHT N
    # messages); reverse to chronological order for the LLM's prompt window.
    chronological = list(reversed(rows))
    return [
        ChannelMessage(
            role="user" if row["direction"] == "inbound" else "assistant",
            body=row["body"],
            timestamp=row["created_at"].isoformat(),
        ).model_dump(mode="json")
        for row in chronological
    ]


async def load_context(state: AgentState) -> dict[str, Any]:
    """Load property/landlord/tenant context, open cases, channel history,
    and weather. Returns a partial state update."""
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])

    property_id = case_context.property_id
    landlord_id = case_context.landlord_id
    tenant_id = case_context.tenant_id

    open_cases: list[dict[str, Any]] = []
    channel_history: list[dict[str, Any]] = []
    vulnerable_occupant: str | None = None
    voice_profile: dict[str, Any] | None = None
    property_row: dict[str, Any] = {}

    async with asynccontextmanager(get_admin_session)() as session:
        if property_id is not None:
            property_row = await _load_property(session, property_id)
        if landlord_id is not None:
            voice_profile = await _load_voice_profile(session, landlord_id)
        if tenant_id is not None:
            vulnerable_occupant = await _load_vulnerable_occupant(session, tenant_id)
            open_cases = await _load_open_cases(session, tenant_id)
            channel_history = await _load_channel_history(session, tenant_id)

    # `model_copy(update=...)` does NOT re-validate (pydantic docs) — a
    # plain DB string would otherwise sit in a field typed `VulnerableOccupant
    # | None` without ever becoming the enum. Coerce explicitly here.
    vulnerable_occupant_enum = (
        VulnerableOccupant(vulnerable_occupant) if vulnerable_occupant is not None else None
    )

    updated_case_context = case_context.model_copy(
        update={
            "house_rules": property_row.get("house_rules"),
            "quiet_hours": property_row.get("quiet_hours"),
            "heating_season": property_row.get("heating_season"),
            "backup_contact": property_row.get("backup_contact"),
            "voice_profile": voice_profile,
            "vulnerable_occupant": vulnerable_occupant_enum,
        }
    )

    log.info(
        "load_context_loaded",
        property_id=str(property_id) if property_id is not None else None,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        open_case_count=len(open_cases),
        channel_history_count=len(channel_history),
    )

    # Warm, plain-English, landlord-facing copy only past this point (rule
    # #8 / CLAUDE.md's "shown on the approval card") — raw counts/ids are
    # already logged above for debugging.
    if tenant_id is not None:
        open_count = len(open_cases)
        if open_count:
            plural = "" if open_count == 1 else "s"
            verb = "is" if open_count == 1 else "are"
            reasoning_log.append(
                f"There {verb} {open_count} other open conversation{plural} with this "
                "tenant right now."
            )
        else:
            reasoning_log.append(
                "I checked this tenant's history — nothing else is open right now."
            )

    lat = property_row.get("lat")
    lon = property_row.get("lon")
    weather = None
    if lat is None or lon is None:
        reasoning_log.append(
            "I don't have a location on file for this property, so I can't check the "
            "weather — I'll assume it could be cold when judging urgency."
        )
    else:
        weather = await get_weather_snapshot(lat, lon)
        log.info(
            "load_context_weather",
            property_id=str(property_id) if property_id is not None else None,
            weather_available=weather is not None,
        )
        if weather is None:
            reasoning_log.append(
                "I couldn't reach the weather service, so I'll assume it could be cold when "
                "judging urgency."
            )
        else:
            reasoning_log.append(
                f"Right now it's {weather.current_temp_c}°C outside, with an overnight low "
                f"of {weather.overnight_low_c}°C."
            )
            if weather.heat_warning:
                reasoning_log.append("There's an active heat warning in the area right now.")

    return {
        "case_context": updated_case_context,
        "open_cases": open_cases,
        "channel_history": channel_history,
        "weather": weather,
        "reasoning_log": reasoning_log,
    }


__all__: list[str] = ["load_context"]
