"""Shared DB-seeding factory helpers for integration tests.

Senior review (asked across PRs #173/#175 and again on #34): "test-factory
duplication now spans 6+ files — extract tests/factories.py as a standalone
follow-up before the suite doubles." This module is that extraction.

Each function takes an already-open ``AsyncSession``, inserts one row
using the same minimal-columns-plus-DB-defaults shape every existing test
module already relied on, commits, and returns the new row's ``id`` as a
``str`` (the convention every existing caller already expects — keeping
that shape means callers don't need to change beyond the import).

Scope: used by this issue's (#34) new test modules
(``test_agent_graph.py``, ``test_agent_degraded_mode.py``,
``test_agent_graph_entry.py``). Migrating the older test modules that
still duplicate these helpers locally (``test_agent_nodes.py``,
``test_agent_classify_intent.py``, ``test_agent_classify_severity.py``,
``test_agent_draft_response.py``, ``test_webhooks_twilio_sms.py``, and
others) is explicitly OUT OF SCOPE for this extraction — a follow-up, not
done unilaterally here.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def fresh_phone() -> str:
    """A syntactically-plausible, collision-free phone number for tests —
    never a real number. Matches every existing test module's own
    duplicated ``_fresh_phone`` helper exactly."""
    return f"+1416{uuid.uuid4().int % 10_000_000:07d}"


async def insert_landlord(
    session: AsyncSession, *, voice_profile: dict[str, Any] | None = None
) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, voice_profile) "
            "VALUES (:id, :auth_id, :email, CAST(:voice_profile AS jsonb))"
        ),
        {
            "id": landlord_id,
            "auth_id": str(uuid.uuid4()),
            "email": f"{landlord_id}@example.com",
            "voice_profile": json.dumps(voice_profile) if voice_profile is not None else None,
        },
    )
    await session.commit()
    return landlord_id


async def insert_property(
    session: AsyncSession,
    landlord_id: str,
    *,
    house_rules: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    property_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO properties (id, landlord_id, label, address_line1, city, house_rules, "
            "lat, lon) "
            "VALUES (:id, :landlord_id, 'Test Property', '123 Test St', 'Toronto', :house_rules, "
            ":lat, :lon)"
        ),
        {
            "id": property_id,
            "landlord_id": landlord_id,
            "house_rules": house_rules,
            "lat": lat,
            "lon": lon,
        },
    )
    await session.commit()
    return property_id


async def insert_tenant(
    session: AsyncSession, landlord_id: str, property_id: str, *, name: str | None = "Maria"
) -> str:
    tenant_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO tenants (id, landlord_id, property_id, phone, name) "
            "VALUES (:id, :landlord_id, :property_id, :phone, :name)"
        ),
        {
            "id": tenant_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "phone": fresh_phone(),
            "name": name,
        },
    )
    await session.commit()
    return tenant_id


async def insert_message(
    session: AsyncSession,
    *,
    landlord_id: str,
    property_id: str,
    tenant_id: str | None,
    body: str = "test message",
    party: str = "tenant",
    prefilter: str | None = None,
) -> str:
    """``tenant_id=None`` seeds the "unknown sender" scenario
    (``identify_property``'s own docstring) — ``party`` still defaults to
    ``'tenant'`` since that is the only party for which an unresolved
    ``tenant_id`` means anything (a ``'landlord'`` party message always
    carries ``tenant_id IS NULL`` by schema, regardless of resolution).
    ``prefilter`` is a pre-serialized JSON string (e.g.
    ``PrefilterResult(...).model_dump_json()``); ``None`` leaves the
    column ``NULL``.
    """
    message_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO messages "
            "(id, landlord_id, property_id, tenant_id, direction, party, body, twilio_sid, "
            " prefilter) "
            "VALUES (:id, :landlord_id, :property_id, :tenant_id, 'inbound', :party, :body, "
            " :twilio_sid, CAST(:prefilter AS jsonb))"
        ),
        {
            "id": message_id,
            "landlord_id": landlord_id,
            "property_id": property_id,
            "tenant_id": tenant_id,
            "party": party,
            "body": body,
            "twilio_sid": f"SM{uuid.uuid4().hex}",
            "prefilter": prefilter,
        },
    )
    await session.commit()
    return message_id


__all__: list[str] = [
    "fresh_phone",
    "insert_landlord",
    "insert_message",
    "insert_property",
    "insert_tenant",
]
