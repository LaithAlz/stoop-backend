"""``identify_case`` node (#110) — message -> case routing + the case
lifecycle transitions that a single inbound message can trigger.

Prefilter invariant (#110 AC: "Prefilter runs on the raw message before
splitting")
------------------------------------------------------------------------
Tier-0 (``app/agent/prefilter.py``) already ran, synchronously, in the
webhook handler, BEFORE this message was even persisted — its result is
snapshotted verbatim onto ``messages.prefilter`` (schema-v1.md). This node
never re-runs ``prefilter.check()`` and never overrides that snapshot: it
reads it once, to decide whether a just-created/matched case's associated
emergency notification (if any) should be backfilled with a ``case_id``
(see "Notification backfill" below). If the snapshot is unexpectedly
missing (should never happen — the webhook always writes one), this node
does NOT raise or block case routing on it: it logs the anomaly
(``log.error``) and falls back to treating the message as no-hard-hit for
the PURPOSES OF THE BACKFILL DECISION ONLY — there is nothing to
de-escalate here, since Tier-0's own result (already acted on, if it fired,
by the webhook) is never touched or reconstructed by this fallback. This is
the concrete meaning of "splitting can never delay an emergency" and "the
agent may escalate a Tier-0 miss, never de-escalate a Tier-0 fire" here:
there IS no de-escalation path in this node, because it never touches the
classification decision at all.

Unknown sender
---------------
``cases.tenant_id`` is ``NOT NULL`` (schema-v1.md) — a case can never be
opened for a message whose sender didn't resolve to a known tenant
(``identify_property``, #30, already logged this and notified the
landlord). This node simply has nothing to route in that case.

The routing seam (deliberately NOT an LLM call in this issue)
-----------------------------------------------------------------
See ``app/agent/case_lifecycle.py``'s module docstring "The routing seam"
for the full rationale. Today, ``_extract_signals`` always returns
``None`` — no node in this codebase populates an intent/multi-issue signal
yet (#31/#32). Once one does (e.g. a future ``state["intents"]`` key),
extend ``_extract_signals`` to read it; ``route_inbound_message`` already
accepts and fully supports the ``signals`` parameter today (unit-tested
directly in ``tests/test_case_lifecycle.py``), so no change to the pure
FSM is needed when that day comes — only this node's wiring.

Notification backfill (``app/routers/webhooks/twilio.py``'s own forward
note: "case_id is NULL throughout: this runs pre-routing (#110 owns case
attach)")
------------------------------------------------------------------------
A Tier-0 HARD hit on a tenant message creates an ``emergency_call``
notification with ``case_id = NULL`` (no case exists yet at webhook time).
Once THIS node determines the case for that same message, it backfills
``notifications.case_id`` (an ordinary, non-append-only table — see
schema-v1.md's RLS grants) so the notification is queryable per-case. The
underlying ``audit_log`` ``emergency_triggered`` row is NOT touched (append
-only, no UPDATE path, ever) — its ``case_id`` stays ``NULL`` forever; the
correlation to a case remains discoverable via ``message_cases`` on the
shared ``message_id``.

``messages`` is append-only — case linkage goes through ``message_cases``
------------------------------------------------------------------------
The webhook always persists ``messages.case_id = NULL`` (case identity
isn't known yet at insert time), and ``messages`` can NEVER be updated
after that (never-break rule #2) — so this node can never retroactively set
``messages.case_id`` either, regardless of how obviously a message belongs
to exactly one case. The durable, ALWAYS-correct link is
``message_cases (message_id, case_id)`` — written here for every case a
message ends up attached to (one row for the common single-issue path,
multiple rows for a future multi-issue split). ``message_cases`` is an
ordinary table (full CRUD for ``app_role``), not append-only.

Tenant-confirmed resolution (schema-v1.md v1.5, migration 0008)
------------------------------------------------------------------
When a future intent signal marks ``tenant_confirms_resolved=True`` on the
issue attached to an EXISTING case, this node persists the 48h auto-apply
deadline directly onto ``cases.pending_resolved_at`` (``propose_resolution``
already computes the APPLY-AT timestamp — see
``app/agent/case_lifecycle.py``'s module docstring). Conversely, whenever a
new message attaches to an existing (still-open) case that ALREADY has a
pending resolution, this node calls ``contradict_resolution`` and clears
the column — deterministic and content-independent: ANY new message on a
pending-resolution case cancels the proposal, never risking a wrongful
auto-close. Neither the proposal nor the contradiction writes an
``audit_log`` entry (unchanged vocabulary, per design) — both are visible
via the column plus ``reasoning_log`` only. Reopening a resolved case
defensively clears the column too (belt-and-braces: it should already be
``NULL`` on any resolved case by invariant); a brand-new case simply never
has one (the column has no DEFAULT, so an omitted ``INSERT`` column is
``NULL``).

DB access
---------
Admin engine (background/graph context) — same pattern as the other two
#30/#110 nodes. Allowlisted in
``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.case_lifecycle import (
    AUDIT_ACTION_CASE_OPENED,
    AUDIT_ACTION_CASE_REOPENED,
    OPEN_STATUSES,
    STATUS_RESOLVED,
    CaseSnapshot,
    OpenCase,
    RoutingSignal,
    contradict_resolution,
    decide_reopen_or_new,
    propose_resolution,
    route_inbound_message,
)
from app.agent.schemas import CaseContext, PrefilterResult
from app.agent.state import AgentState
from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

_OPEN_STATUS_LITERAL_LIST = ", ".join(f"'{status}'" for status in sorted(OPEN_STATUSES))

_SELECT_MESSAGE_SQL = text(
    "SELECT m.id, m.tenant_id, m.landlord_id, m.property_id, m.prefilter, t.name AS tenant_name "
    "FROM messages m "
    "LEFT JOIN tenants t ON t.id = m.tenant_id "
    "WHERE m.id = :message_id"
)

_SELECT_OPEN_CASES_SQL = text(
    "SELECT id, last_activity_at FROM cases "  # noqa: S608
    f"WHERE tenant_id = :tenant_id AND status IN ({_OPEN_STATUS_LITERAL_LIST}) "
    "ORDER BY last_activity_at DESC"
    # ^ IN-list built from the internal OPEN_STATUSES constant only, never
    #   external/request-supplied data — not a real injection vector.
)

_SELECT_CASE_SQL = text(
    "SELECT id, status, resolved_reason, resolved_at, last_activity_at, pending_resolved_at "
    "FROM cases WHERE id = :case_id"
)

_INSERT_CASE_SQL = text(
    """
    INSERT INTO cases (
        landlord_id, property_id, tenant_id, status, langgraph_thread_id,
        related_case_id, title
    )
    VALUES (:landlord_id, :property_id, :tenant_id, 'open', :thread_id, :related_case_id, :title)
    RETURNING id
    """
)

_INSERT_CASE_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', :action, CAST(:payload AS jsonb))"
)

_REOPEN_CASE_SQL = text(
    "UPDATE cases SET status = 'reopened', resolved_reason = NULL, resolved_at = NULL, "
    "pending_resolved_at = NULL, last_activity_at = :now, updated_at = :now WHERE id = :case_id"
)

_BUMP_ACTIVITY_SQL = text(
    "UPDATE cases SET last_activity_at = :now, updated_at = :now WHERE id = :case_id"
)

_LINK_MESSAGE_TO_CASE_SQL = text(
    "INSERT INTO message_cases (message_id, case_id) VALUES (:message_id, :case_id) "
    "ON CONFLICT (message_id, case_id) DO NOTHING"
)

_BACKFILL_NOTIFICATION_CASE_ID_SQL = text(
    "UPDATE notifications SET case_id = :case_id "
    "WHERE payload ->> 'message_id' = :message_id AND case_id IS NULL"
)

# Schema-v1.md v1.5 (migration 0008) — the tenant-confirmed-resolution
# timer. See app/agent/case_lifecycle.py's module docstring for the full
# design (APPLY-AT semantics, precedence over auto-stale, unchanged audit
# vocabulary).
_SET_PENDING_RESOLUTION_SQL = text(
    "UPDATE cases SET pending_resolved_at = :deadline, updated_at = :now WHERE id = :case_id"
)

_CLEAR_PENDING_RESOLUTION_SQL = text(
    "UPDATE cases SET pending_resolved_at = NULL, updated_at = :now WHERE id = :case_id"
)


def _extract_signals(state: AgentState) -> list[RoutingSignal] | None:
    """The LLM-informed routing seam — see module docstring "The routing
    seam". Always ``None`` today: no node populates a multi-issue/intent
    signal in ``state`` yet (#31/#32). ``route_inbound_message`` already
    supports this parameter fully; only this function needs to change once
    a future node adds e.g. ``state["intents"]``.
    """
    return None


def _parse_prefilter(raw: Any) -> PrefilterResult:
    if raw is None:
        log.error("identify_case_prefilter_snapshot_missing")
        return PrefilterResult(hard_hit=False)
    return PrefilterResult.model_validate(raw)


async def _open_new_case(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    property_id: UUID,
    tenant_id: UUID,
    related_case_id: UUID | None,
    title: str | None,
) -> UUID:
    row = (
        (
            await session.execute(
                _INSERT_CASE_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "property_id": str(property_id),
                    "tenant_id": str(tenant_id),
                    "thread_id": str(uuid4()),
                    "related_case_id": str(related_case_id) if related_case_id else None,
                    "title": title,
                },
            )
        )
        .mappings()
        .one()
    )
    case_id: UUID = row["id"]
    await session.execute(
        _INSERT_CASE_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "case_id": str(case_id),
            "action": AUDIT_ACTION_CASE_OPENED,
            "payload": json.dumps(
                {"related_case_id": str(related_case_id) if related_case_id else None}
            ),
        },
    )
    return case_id


async def _reopen_case(
    session: AsyncSession, *, case_id: UUID, landlord_id: UUID, now: datetime
) -> UUID:
    await session.execute(_REOPEN_CASE_SQL, {"now": now, "case_id": str(case_id)})
    await session.execute(
        _INSERT_CASE_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "case_id": str(case_id),
            "action": AUDIT_ACTION_CASE_REOPENED,
            "payload": json.dumps({}),
        },
    )
    return case_id


async def _bump_activity(session: AsyncSession, *, case_id: UUID, now: datetime) -> None:
    await session.execute(_BUMP_ACTIVITY_SQL, {"now": now, "case_id": str(case_id)})


async def _set_pending_resolution(
    session: AsyncSession, *, case_id: UUID, deadline: datetime, now: datetime
) -> None:
    await session.execute(
        _SET_PENDING_RESOLUTION_SQL, {"deadline": deadline, "now": now, "case_id": str(case_id)}
    )


async def _clear_pending_resolution(session: AsyncSession, *, case_id: UUID, now: datetime) -> None:
    await session.execute(_CLEAR_PENDING_RESOLUTION_SQL, {"now": now, "case_id": str(case_id)})


async def _link_message_to_case(session: AsyncSession, *, message_id: UUID, case_id: UUID) -> None:
    await session.execute(
        _LINK_MESSAGE_TO_CASE_SQL, {"message_id": str(message_id), "case_id": str(case_id)}
    )


async def _backfill_notification_case_id(
    session: AsyncSession, *, message_id: UUID, case_id: UUID
) -> None:
    await session.execute(
        _BACKFILL_NOTIFICATION_CASE_ID_SQL,
        {"message_id": str(message_id), "case_id": str(case_id)},
    )


async def identify_case(state: AgentState) -> dict[str, Any]:
    """Route the inbound message to case(s), applying the reopen window
    and (when a future signal marks it) the tenant-confirmed-resolution
    proposal. Returns a partial state update."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    now = datetime.now(UTC)

    async with asynccontextmanager(get_admin_session)() as session:
        message_row = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one()
        )
        prefilter_result = _parse_prefilter(message_row["prefilter"])
        log.info(
            "identify_case_prefilter_checked",
            message_id=str(message_id),
            hard_hit=prefilter_result.hard_hit,
        )

        tenant_id: UUID | None = message_row["tenant_id"]
        landlord_id: UUID = message_row["landlord_id"]
        property_id: UUID = message_row["property_id"]
        tenant_name: str | None = message_row["tenant_name"]

        if tenant_id is None:
            reasoning_log.append(
                "I still don't recognize this sender, so I couldn't attach this message to a "
                "conversation."
            )
            return {"case_context": case_context, "reasoning_log": reasoning_log}

        open_case_rows = (
            (await session.execute(_SELECT_OPEN_CASES_SQL, {"tenant_id": str(tenant_id)}))
            .mappings()
            .all()
        )
        open_cases = [
            OpenCase(case_id=row["id"], last_activity_at=row["last_activity_at"])
            for row in open_case_rows
        ]

        signals = _extract_signals(state)
        results = route_inbound_message(
            open_cases=open_cases, signals=signals, tenant_label=tenant_name
        )

        primary_case_id: UUID | None = None
        for result in results:
            reasoning_log.extend(result.reasoning_log)

            if result.action == "chitchat":
                continue

            case_id: UUID
            if result.action == "new_case":
                case_id = await _open_new_case(
                    session,
                    landlord_id=landlord_id,
                    property_id=property_id,
                    tenant_id=tenant_id,
                    related_case_id=None,
                    title=None,
                )
            elif result.action == "attach_existing":
                if result.target_case_id is None:  # pragma: no cover — routing invariant
                    raise ValueError(
                        "route_inbound_message returned action='attach_existing' with no "
                        "target_case_id — routing invariant violated"
                    )
                case_row = (
                    (
                        await session.execute(
                            _SELECT_CASE_SQL, {"case_id": str(result.target_case_id)}
                        )
                    )
                    .mappings()
                    .one()
                )
                if case_row["status"] == STATUS_RESOLVED:
                    snapshot = CaseSnapshot(
                        case_id=case_row["id"],
                        status=case_row["status"],
                        resolved_reason=case_row["resolved_reason"],
                        resolved_at=case_row["resolved_at"],
                        last_activity_at=case_row["last_activity_at"],
                    )
                    reopen_decision = decide_reopen_or_new(snapshot, now)
                    reasoning_log.append(reopen_decision.reasoning_log)
                    log.info(
                        "identify_case_reopen_decision",
                        case_id=str(case_row["id"]),
                        reopen=reopen_decision.reopen,
                    )
                    if reopen_decision.reopen:
                        case_id = await _reopen_case(
                            session, case_id=case_row["id"], landlord_id=landlord_id, now=now
                        )
                    else:
                        case_id = await _open_new_case(
                            session,
                            landlord_id=landlord_id,
                            property_id=property_id,
                            tenant_id=tenant_id,
                            related_case_id=case_row["id"],
                            title=None,
                        )
                else:
                    case_id = case_row["id"]
                    await _bump_activity(session, case_id=case_id, now=now)

                    # A new message on a case with a PENDING tenant-confirmed
                    # resolution is a contradiction (schema-v1.md v1.5,
                    # migration 0008; app/agent/case_lifecycle.py's module
                    # docstring "Precedence"/"Design choice") — cancel it
                    # deterministically, content-independent (see
                    # contradict_resolution's own docstring).
                    if case_row["pending_resolved_at"] is not None:
                        open_snapshot = CaseSnapshot(
                            case_id=case_row["id"],
                            status=case_row["status"],
                            resolved_reason=case_row["resolved_reason"],
                            resolved_at=case_row["resolved_at"],
                            last_activity_at=case_row["last_activity_at"],
                            pending_resolved_at=case_row["pending_resolved_at"],
                        )
                        contradiction = contradict_resolution(open_snapshot)
                        reasoning_log.append(contradiction.reasoning_log)
                        log.info(
                            "identify_case_contradiction_checked",
                            case_id=str(case_id),
                            cleared=contradiction.cleared,
                        )
                        if contradiction.cleared:
                            await _clear_pending_resolution(session, case_id=case_id, now=now)
            else:  # pragma: no cover — defensive, route_inbound_message never returns anything else
                continue

            await _link_message_to_case(session, message_id=message_id, case_id=case_id)

            if result.tenant_confirms_resolved:
                proposal = propose_resolution(now)
                await _set_pending_resolution(
                    session, case_id=case_id, deadline=proposal.pending_resolved_at, now=now
                )
                reasoning_log.append(proposal.reasoning_log)
                log.info(
                    "identify_case_resolution_proposed",
                    case_id=str(case_id),
                    pending_resolved_at=proposal.pending_resolved_at.isoformat(),
                )

            if primary_case_id is None:
                primary_case_id = case_id

        if prefilter_result.hard_hit and primary_case_id is not None:
            await _backfill_notification_case_id(
                session, message_id=message_id, case_id=primary_case_id
            )

        if primary_case_id is not None:
            case_context = case_context.model_copy(update={"case_id": primary_case_id})

    return {"case_context": case_context, "reasoning_log": reasoning_log}


__all__: list[str] = ["identify_case"]
