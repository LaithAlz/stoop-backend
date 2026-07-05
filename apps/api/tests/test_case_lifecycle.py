"""Unit tests for app/agent/case_lifecycle.py — pure state-machine
functions (#110).

Markers: ``@pytest.mark.unit`` — all tests here are pure, no I/O, no DB.
Covers every lifecycle transition + the reopen boundary at 29d/30d/31d, per
the issue's own test requirements.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.case_lifecycle import (
    AUDIT_ACTION_CASE_RESOLVED,
    RESOLUTION_PROPOSAL_WINDOW,
    STATUS_AWAITING_APPROVAL,
    STATUS_AWAITING_TENANT,
    STATUS_OPEN,
    STATUS_RESOLVED,
    CaseSnapshot,
    OpenCase,
    RoutingSignal,
    apply_time_transitions,
    contradict_resolution,
    decide_reopen_or_new,
    propose_resolution,
    resolve_by_landlord,
    route_inbound_message,
)

_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _snapshot(
    *,
    status: str = STATUS_OPEN,
    resolved_reason: str | None = None,
    resolved_at: datetime | None = None,
    last_activity_at: datetime = _NOW,
    pending_resolved_at: datetime | None = None,
    case_id: uuid.UUID | None = None,
) -> CaseSnapshot:
    return CaseSnapshot(
        case_id=case_id or uuid.uuid4(),
        status=status,
        resolved_reason=resolved_reason,
        resolved_at=resolved_at,
        last_activity_at=last_activity_at,
        pending_resolved_at=pending_resolved_at,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_routing_no_open_cases_opens_new_case() -> None:
    results = route_inbound_message(open_cases=[])
    assert len(results) == 1
    assert results[0].action == "new_case"
    assert results[0].target_case_id is None


@pytest.mark.unit
def test_routing_exactly_one_open_case_attaches_unambiguously() -> None:
    case_id = uuid.uuid4()
    results = route_inbound_message(open_cases=[OpenCase(case_id=case_id, last_activity_at=_NOW)])
    assert len(results) == 1
    assert results[0].action == "attach_existing"
    assert results[0].target_case_id == case_id
    assert "continues the open conversation" in results[0].reasoning_log[0]


@pytest.mark.unit
def test_routing_exactly_one_open_case_uses_tenant_label_when_given() -> None:
    case_id = uuid.uuid4()
    results = route_inbound_message(
        open_cases=[OpenCase(case_id=case_id, last_activity_at=_NOW)], tenant_label="Maria"
    )
    assert "Maria's open conversation" in results[0].reasoning_log[0]


@pytest.mark.unit
def test_routing_multiple_open_cases_attaches_to_most_recent_with_ambiguity_note() -> None:
    older = OpenCase(case_id=uuid.uuid4(), last_activity_at=_NOW - timedelta(days=2))
    newer = OpenCase(case_id=uuid.uuid4(), last_activity_at=_NOW - timedelta(hours=1))
    middle = OpenCase(case_id=uuid.uuid4(), last_activity_at=_NOW - timedelta(days=1))

    results = route_inbound_message(open_cases=[older, newer, middle])

    assert len(results) == 1
    assert results[0].action == "attach_existing"
    assert results[0].target_case_id == newer.case_id
    assert "more than one open conversation" in results[0].reasoning_log[0]
    # No jargon: no node-name prefix, no raw ids, plain sentence.
    assert "identify_case" not in results[0].reasoning_log[0]
    assert str(newer.case_id) not in results[0].reasoning_log[0]


@pytest.mark.unit
def test_routing_signals_chitchat() -> None:
    signals = [RoutingSignal(is_new_issue=False, is_chitchat=True)]
    results = route_inbound_message(open_cases=[], signals=signals)
    assert len(results) == 1
    assert results[0].action == "chitchat"
    assert results[0].target_case_id is None


@pytest.mark.unit
def test_routing_signals_matched_existing_case() -> None:
    matched_id = uuid.uuid4()
    signals = [RoutingSignal(is_new_issue=False, matched_case_id=matched_id, summary="heat update")]
    results = route_inbound_message(open_cases=[], signals=signals)
    assert len(results) == 1
    assert results[0].action == "attach_existing"
    assert results[0].target_case_id == matched_id
    assert "heat update" in results[0].reasoning_log[0]


@pytest.mark.unit
def test_routing_signals_new_issue() -> None:
    signals = [RoutingSignal(is_new_issue=True, summary="bathroom fan broken")]
    results = route_inbound_message(open_cases=[], signals=signals)
    assert len(results) == 1
    assert results[0].action == "new_case"
    assert results[0].target_case_id is None
    assert "bathroom fan broken" in results[0].reasoning_log[0]


@pytest.mark.unit
def test_routing_signals_multi_issue_split() -> None:
    """Multi-issue message -- one signal continues an existing case, the
    other opens a new one (conversation-model.md, "Multi-issue")."""
    existing_id = uuid.uuid4()
    signals = [
        RoutingSignal(is_new_issue=False, matched_case_id=existing_id, summary="heat still out"),
        RoutingSignal(is_new_issue=True, summary="bathroom fan broken"),
    ]
    results = route_inbound_message(open_cases=[], signals=signals)
    assert len(results) == 2
    assert results[0].action == "attach_existing"
    assert results[0].target_case_id == existing_id
    assert results[1].action == "new_case"


@pytest.mark.unit
def test_routing_signals_tenant_confirms_resolved_passthrough() -> None:
    matched_id = uuid.uuid4()
    signals = [
        RoutingSignal(is_new_issue=False, matched_case_id=matched_id, tenant_confirms_resolved=True)
    ]
    results = route_inbound_message(open_cases=[], signals=signals)
    assert results[0].tenant_confirms_resolved is True


# ---------------------------------------------------------------------------
# Reopen window boundary — 29d / 30d / 31d
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reopen_within_29_days_reopens_same_case() -> None:
    resolved_at = _NOW - timedelta(days=29)
    snapshot = _snapshot(
        status=STATUS_RESOLVED, resolved_reason="tenant_confirmed", resolved_at=resolved_at
    )
    decision = decide_reopen_or_new(snapshot, _NOW)
    assert decision.reopen is True


@pytest.mark.unit
def test_reopen_at_exactly_30_days_reopens_same_case() -> None:
    """Boundary judgment call (documented in case_lifecycle.py): exactly
    30 days is treated as still within the reopen window (inclusive)."""
    resolved_at = _NOW - timedelta(days=30)
    snapshot = _snapshot(
        status=STATUS_RESOLVED, resolved_reason="landlord", resolved_at=resolved_at
    )
    decision = decide_reopen_or_new(snapshot, _NOW)
    assert decision.reopen is True


@pytest.mark.unit
def test_reopen_at_31_days_opens_new_related_case() -> None:
    resolved_at = _NOW - timedelta(days=31)
    snapshot = _snapshot(
        status=STATUS_RESOLVED, resolved_reason="auto_stale", resolved_at=resolved_at
    )
    decision = decide_reopen_or_new(snapshot, _NOW)
    assert decision.reopen is False


@pytest.mark.unit
def test_reopen_rejects_non_resolved_case() -> None:
    snapshot = _snapshot(status=STATUS_OPEN)
    with pytest.raises(ValueError, match="non-resolved"):
        decide_reopen_or_new(snapshot, _NOW)


@pytest.mark.unit
def test_reopen_rejects_resolved_case_without_resolved_at() -> None:
    snapshot = _snapshot(status=STATUS_RESOLVED, resolved_reason="landlord", resolved_at=None)
    with pytest.raises(ValueError, match="resolved_at"):
        decide_reopen_or_new(snapshot, _NOW)


# ---------------------------------------------------------------------------
# Landlord-direct resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_by_landlord() -> None:
    transition = resolve_by_landlord(_NOW)
    assert transition.new_status == STATUS_RESOLVED
    assert transition.resolved_reason == "landlord"
    assert transition.resolved_at == _NOW
    assert transition.audit_action == AUDIT_ACTION_CASE_RESOLVED


# ---------------------------------------------------------------------------
# Tenant-confirmed proposal / contradiction / auto-apply
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_propose_resolution() -> None:
    """propose_resolution bakes the 48h window in AT PROPOSAL TIME: the
    returned value is the APPLY-AT deadline (now + window), not `now`
    itself — see case_lifecycle.py's module docstring "Design choice"."""
    result = propose_resolution(_NOW)
    assert result.pending_resolved_at == _NOW + RESOLUTION_PROPOSAL_WINDOW
    assert "48 hours" in result.reasoning_log
    # Warm, plain English -- no jargon.
    assert "identify_case" not in result.reasoning_log


@pytest.mark.unit
def test_contradict_resolution_clears_pending() -> None:
    snapshot = _snapshot(
        status=STATUS_AWAITING_TENANT, pending_resolved_at=_NOW + timedelta(hours=1)
    )
    result = contradict_resolution(snapshot)
    assert result.cleared is True


@pytest.mark.unit
def test_contradict_resolution_noop_when_nothing_pending() -> None:
    snapshot = _snapshot(status=STATUS_AWAITING_TENANT, pending_resolved_at=None)
    result = contradict_resolution(snapshot)
    assert result.cleared is False


@pytest.mark.unit
def test_apply_time_transitions_auto_applies_when_deadline_reached() -> None:
    """`pending_resolved_at` is the APPLY-AT time -- `now >= deadline` fires,
    regardless of how it got there (48h after proposal, or any other time)."""
    snapshot = _snapshot(
        status=STATUS_AWAITING_TENANT,
        pending_resolved_at=_NOW - timedelta(minutes=1),
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert len(actions) == 1
    assert actions[0].case_id == snapshot.case_id
    assert actions[0].transition.new_status == STATUS_RESOLVED
    assert actions[0].transition.resolved_reason == "tenant_confirmed"


@pytest.mark.unit
def test_apply_time_transitions_auto_applies_at_exact_deadline() -> None:
    """Inclusive boundary: `now == pending_resolved_at` applies (`>=`)."""
    snapshot = _snapshot(status=STATUS_AWAITING_TENANT, pending_resolved_at=_NOW)
    actions = apply_time_transitions([snapshot], _NOW)
    assert len(actions) == 1
    assert actions[0].transition.resolved_reason == "tenant_confirmed"


@pytest.mark.unit
def test_apply_time_transitions_does_not_auto_apply_before_deadline() -> None:
    snapshot = _snapshot(
        status=STATUS_AWAITING_TENANT,
        pending_resolved_at=_NOW + timedelta(minutes=1),
        last_activity_at=_NOW,
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert actions == []


@pytest.mark.unit
def test_apply_time_transitions_pending_not_due_prevents_auto_stale_even_when_very_inactive() -> (
    None
):
    """Precedence (case_lifecycle.py's module docstring "Precedence over
    the 14-day auto-stale sweep"): a case with a pending resolution NOT yet
    due must NOT auto-stale, no matter how old its last_activity_at is —
    it must simply be left untouched until the deadline arrives (or a new
    message contradicts it)."""
    snapshot = _snapshot(
        status=STATUS_AWAITING_TENANT,
        pending_resolved_at=_NOW + timedelta(hours=1),
        last_activity_at=_NOW - timedelta(days=100),
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert actions == []


# ---------------------------------------------------------------------------
# Auto-stale sweep
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_time_transitions_auto_stale_at_exactly_14_days() -> None:
    snapshot = _snapshot(status=STATUS_OPEN, last_activity_at=_NOW - timedelta(days=14))
    actions = apply_time_transitions([snapshot], _NOW)
    assert len(actions) == 1
    assert actions[0].transition.resolved_reason == "auto_stale"
    assert actions[0].transition.new_status == STATUS_RESOLVED


@pytest.mark.unit
def test_apply_time_transitions_no_auto_stale_before_14_days() -> None:
    snapshot = _snapshot(
        status=STATUS_OPEN, last_activity_at=_NOW - (timedelta(days=14) - timedelta(minutes=1))
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert actions == []


@pytest.mark.unit
def test_apply_time_transitions_auto_stale_past_14_days() -> None:
    snapshot = _snapshot(status=STATUS_AWAITING_TENANT, last_activity_at=_NOW - timedelta(days=20))
    actions = apply_time_transitions([snapshot], _NOW)
    assert len(actions) == 1
    assert actions[0].transition.resolved_reason == "auto_stale"


@pytest.mark.unit
def test_apply_time_transitions_ignores_already_resolved_cases() -> None:
    snapshot = _snapshot(
        status=STATUS_RESOLVED,
        resolved_reason="landlord",
        resolved_at=_NOW - timedelta(days=100),
        last_activity_at=_NOW - timedelta(days=100),
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert actions == []


@pytest.mark.unit
def test_apply_time_transitions_prefers_pending_resolution_over_auto_stale() -> None:
    """A case that is BOTH past its pending-resolution deadline and would
    separately qualify for 14-day auto-stale (e.g. its last_activity_at
    happens to be old too) must resolve via the more specific
    tenant-confirmed path, not auto_stale."""
    snapshot = _snapshot(
        status=STATUS_AWAITING_TENANT,
        pending_resolved_at=_NOW - timedelta(hours=1),
        last_activity_at=_NOW - timedelta(days=20),
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert len(actions) == 1
    assert actions[0].transition.resolved_reason == "tenant_confirmed"


@pytest.mark.unit
def test_apply_time_transitions_never_auto_stales_awaiting_approval() -> None:
    """A landlord's unactioned draft (`awaiting_approval`) must never
    self-resolve, no matter how inactive -- that would silently hide their
    own backlog rather than surface it (#110 review)."""
    snapshot = _snapshot(
        status=STATUS_AWAITING_APPROVAL, last_activity_at=_NOW - timedelta(days=100)
    )
    actions = apply_time_transitions([snapshot], _NOW)
    assert actions == []


@pytest.mark.unit
def test_apply_time_transitions_multiple_cases_mixed_outcomes() -> None:
    stale = _snapshot(status=STATUS_OPEN, last_activity_at=_NOW - timedelta(days=15))
    fresh = _snapshot(status=STATUS_OPEN, last_activity_at=_NOW - timedelta(hours=1))
    resolved = _snapshot(
        status=STATUS_RESOLVED, resolved_reason="landlord", resolved_at=_NOW - timedelta(days=1)
    )

    actions = apply_time_transitions([stale, fresh, resolved], _NOW)

    assert {a.case_id for a in actions} == {stale.case_id}
