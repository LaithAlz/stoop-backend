"""Unit tests for app/agent/schemas.py and app/agent/state.py.

Covers:
- Enum value sets match the rubric / prompt keys / schema-v1.md constraints.
- Every Pydantic model round-trips: construct → model_dump() → re-validate.
- extra="forbid" rejects unknown fields on all models.
- AgentState TypedDict can be constructed with a representative payload and
  reasoning_log accumulates correctly.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.schemas import (
    CaseContext,
    ChannelMessage,
    DraftResult,
    Intent,
    IntentResult,
    PrefilterResult,
    RefusalFlag,
    Severity,
    SeverityResult,
    VulnerableOccupant,
    WeatherSnapshot,
)
from app.agent.state import AgentState

# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_values() -> None:
    """Severity enum values must match rubric v1.0 output tokens exactly."""
    assert set(s.value for s in Severity) == {"EMERGENCY", "URGENT", "ROUTINE"}


@pytest.mark.unit
def test_severity_db_value_is_lowercase() -> None:
    """db_value bridges the UPPERCASE rubric token to the lowercase DB CHECK
    (schema-v1: CHECK (severity IN ('emergency','urgent','routine')))."""
    assert Severity.EMERGENCY.db_value == "emergency"
    assert Severity.URGENT.db_value == "urgent"
    assert Severity.ROUTINE.db_value == "routine"
    # The set of db_values must equal the schema-v1 CHECK set exactly.
    assert {s.db_value for s in Severity} == {"emergency", "urgent", "routine"}


@pytest.mark.unit
def test_severity_rejects_lowercase_token() -> None:
    """Guard against anyone 'harmonizing' the enum to the DB casing: the enum
    parses the rubric's UPPERCASE output tokens, so lowercase must be invalid."""
    with pytest.raises(ValueError):
        Severity("emergency")


@pytest.mark.unit
def test_refusal_flag_values_match_prompt_keys() -> None:
    """RefusalFlag values must be identical to REFUSAL_TEMPLATES keys in
    EVERY prompt package version (v1 frozen, v2 live)."""
    from app.agent.prompts.v1 import REFUSAL_TEMPLATES as TEMPLATES_V1
    from app.agent.prompts.v2 import REFUSAL_TEMPLATES as TEMPLATES_V2

    flag_values = {f.value for f in RefusalFlag}
    for version, templates in (("v1", TEMPLATES_V1), ("v2", TEMPLATES_V2)):
        template_keys = set(templates.keys())
        assert flag_values == template_keys, (
            f"RefusalFlag values and {version} REFUSAL_TEMPLATES keys diverged.\n"
            f"  RefusalFlag : {sorted(flag_values)}\n"
            f"  REFUSAL_TEMPLATES: {sorted(template_keys)}\n"
            f"Update schemas.py or prompts/{version}.py so they match."
        )


@pytest.mark.unit
def test_prompts_v2_changes_exactly_the_founder_approved_templates() -> None:
    """Pin the founder-approved v2 diff (2026-07-06): four templates
    rewritten for plain-language conformance, other_tenants byte-identical
    to v1, and the system-prompt builders re-exported unchanged from v1."""
    from app.agent.prompts import v1, v2

    assert v2.PROMPT_VERSION == "v2"
    assert v1.PROMPT_VERSION == "v1"
    changed = {
        key
        for key in v1.REFUSAL_TEMPLATES
        if v1.REFUSAL_TEMPLATES[key] != v2.REFUSAL_TEMPLATES[key]
    }
    assert changed == {
        "access_codes",
        "legal_rent_ltb",
        "cost_compensation",
        "impersonation",
    }
    assert v2.REFUSAL_TEMPLATES["other_tenants"] == v1.REFUSAL_TEMPLATES["other_tenants"]
    # Byte-identical by construction: v2 re-exports the frozen v1 builders.
    assert v2.get_classify_system_prompt is v1.get_classify_system_prompt
    assert v2.build_draft_system_prompt is v1.build_draft_system_prompt
    # The v1 legalistic phrasing that failed eval gates 5-7 is gone from v2.
    assert "Landlord and Tenant Board" not in v2.REFUSAL_TEMPLATES["legal_rent_ltb"]
    assert "on their behalf" not in v2.REFUSAL_TEMPLATES["legal_rent_ltb"]
    assert "on their behalf" not in v2.REFUSAL_TEMPLATES["impersonation"]


@pytest.mark.unit
def test_refusal_flag_exact_values() -> None:
    """RefusalFlag has exactly the five documented refusal topics."""
    expected = {
        "access_codes",
        "legal_rent_ltb",
        "cost_compensation",
        "other_tenants",
        "impersonation",
    }
    assert {f.value for f in RefusalFlag} == expected


@pytest.mark.unit
def test_vulnerable_occupant_values_match_schema() -> None:
    """VulnerableOccupant values must match tenants.vulnerable_occupant CHECK."""
    # schema-v1.md: CHECK (vulnerable_occupant IN ('infant','elderly','medical_device'))
    expected = {"infant", "elderly", "medical_device"}
    assert {v.value for v in VulnerableOccupant} == expected


@pytest.mark.unit
def test_intent_values_match_schema() -> None:
    """Intent values must match cases.intent domain in schema-v1.md."""
    # schema-v1.md: -- intent text,  -- maintenance|admin|question|other
    expected = {"maintenance", "admin", "question", "other"}
    assert {i.value for i in Intent} == expected


# ---------------------------------------------------------------------------
# SeverityResult round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_result_round_trip_minimal() -> None:
    """Minimal SeverityResult round-trips through model_dump → revalidate."""
    original = SeverityResult(severity=Severity.ROUTINE)
    dumped: dict[str, Any] = original.model_dump()
    restored = SeverityResult.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_severity_result_round_trip_full() -> None:
    """Full SeverityResult with all optional fields round-trips correctly."""
    original = SeverityResult(
        severity=Severity.EMERGENCY,
        rules_fired=["burst pipe — active uncontained water", "water contacting electrical"],
        modifier="vulnerable-occupant bump: infant — URGENT → EMERGENCY",
        refusal_flags=[RefusalFlag.access_codes],
        reasoning=["Active burst pipe is an uncontained water hazard."],
    )
    dumped = original.model_dump()
    restored = SeverityResult.model_validate(dumped)
    assert restored == original
    assert restored.severity is Severity.EMERGENCY
    assert RefusalFlag.access_codes in restored.refusal_flags


@pytest.mark.unit
def test_severity_result_rejects_extra_fields() -> None:
    """extra='forbid' must reject unknown fields."""
    with pytest.raises(ValidationError):
        SeverityResult(severity=Severity.URGENT, unexpected_field="bad")  # type: ignore[call-arg]


@pytest.mark.unit
def test_severity_result_rejects_invalid_severity() -> None:
    """SeverityResult rejects severity values not in the Severity enum."""
    with pytest.raises(ValidationError):
        SeverityResult(severity="CRITICAL")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SeverityResult singular -> list coercion (paid eval gate finding,
# 2026-07-05: F1's real run got `reasoning` back as a bare string once)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_result_coerces_bare_reasoning_string_to_list() -> None:
    result = SeverityResult.model_validate(
        {
            "severity": "ROUTINE",
            "reasoning": "Tenant references a past dispute; flagged for landlord review.",
        }
    )
    assert result.reasoning == ["Tenant references a past dispute; flagged for landlord review."]


@pytest.mark.unit
def test_severity_result_coerces_bare_rules_fired_string_to_list() -> None:
    result = SeverityResult.model_validate(
        {"severity": "URGENT", "rules_fired": "No heat (outdoor temp above -10C)"}
    )
    assert result.rules_fired == ["No heat (outdoor temp above -10C)"]


@pytest.mark.unit
def test_severity_result_coerces_bare_refusal_flag_string_to_list() -> None:
    result = SeverityResult.model_validate({"severity": "ROUTINE", "refusal_flags": "access_codes"})
    assert result.refusal_flags == [RefusalFlag.access_codes]


@pytest.mark.unit
def test_severity_result_real_list_input_unaffected_by_coercion() -> None:
    """The coercion must never touch an already-correct list -- this is a
    before-validator safety net, not a behavior change for the normal
    (already-a-list) case."""
    result = SeverityResult.model_validate(
        {
            "severity": "EMERGENCY",
            "rules_fired": ["a", "b"],
            "refusal_flags": ["legal_rent_ltb"],
            "reasoning": ["one", "two"],
        }
    )
    assert result.rules_fired == ["a", "b"]
    assert result.refusal_flags == [RefusalFlag.legal_rent_ltb]
    assert result.reasoning == ["one", "two"]


@pytest.mark.unit
def test_severity_result_coercion_still_validates_enum_membership() -> None:
    """A bare, but INVALID, refusal-flag string still raises -- the
    coercion only unwraps the string into a list; it does not bypass
    normal enum validation."""
    with pytest.raises(ValidationError):
        SeverityResult.model_validate({"severity": "ROUTINE", "refusal_flags": "not_a_real_flag"})


# ---------------------------------------------------------------------------
# SeverityResult single-key wrapper unwrapping (paid eval gate finding,
# 2026-07-05: the model sometimes nests the payload under an extra key --
# observed live: {"severity_result": {...}} and {"severity_input": {...}})
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_severity_result_unwraps_severity_result_wrapper_key() -> None:
    """Observed live wrapper shape #1: {"severity_result": {...}}."""
    result = SeverityResult.model_validate(
        {
            "severity_result": {
                "severity": "EMERGENCY",
                "rules_fired": ["Gas smell"],
                "refusal_flags": [],
                "reasoning": "Gas smell reported; tenant told to leave and call 911 from outside.",
            }
        }
    )
    assert result.severity == Severity.EMERGENCY
    assert result.rules_fired == ["Gas smell"]
    # The wrapper unwrap AND the str -> list coercion both fired here --
    # they compose (model-level unwrap runs before field-level coercion).
    assert result.reasoning == [
        "Gas smell reported; tenant told to leave and call 911 from outside."
    ]


@pytest.mark.unit
def test_severity_result_unwraps_severity_input_wrapper_key() -> None:
    """Observed live wrapper shape #2: {"severity_input": {...}}."""
    result = SeverityResult.model_validate(
        {
            "severity_input": {
                "severity": "ROUTINE",
                "rules_fired": [],
                "refusal_flags": ["legal_rent_ltb"],
                "reasoning": ["Tenant asked about rent receipts."],
            }
        }
    )
    assert result.severity == Severity.ROUTINE
    assert result.refusal_flags == [RefusalFlag.legal_rent_ltb]


@pytest.mark.unit
def test_severity_result_flat_payload_untouched_by_unwrap() -> None:
    """A correct, already-flat payload -- including one where only a
    SINGLE field is set (the tricky edge case: a single-key dict whose one
    key IS a real field name) -- must never be treated as a wrapper."""
    result = SeverityResult.model_validate({"severity": "ROUTINE"})
    assert result.severity == Severity.ROUTINE
    assert result.rules_fired == []
    assert result.refusal_flags == []
    assert result.reasoning == []


@pytest.mark.unit
def test_severity_result_nonsense_single_key_dict_still_fails() -> None:
    """A single-key dict whose inner dict has NO recognized field names is
    a genuinely wrong payload -- it must NOT be unwrapped, and must still
    fail validation loudly (never silently "rescued")."""
    with pytest.raises(ValidationError):
        SeverityResult.model_validate({"some_random_key": {"totally": "unrelated", "data": 1}})


@pytest.mark.unit
def test_severity_result_single_key_dict_with_non_dict_value_not_unwrapped() -> None:
    """A single-key dict whose value is NOT a dict at all (e.g. a bare
    string) must not be treated as a wrapper either."""
    with pytest.raises(ValidationError):
        SeverityResult.model_validate({"severity_result": "EMERGENCY"})


@pytest.mark.unit
def test_severity_result_wrapper_plus_bare_string_reasoning_compose() -> None:
    """Both robustness layers fire together: the payload is wrapped AND
    its inner ``reasoning`` is a bare string -- both must be corrected."""
    result = SeverityResult.model_validate(
        {
            "severity_result": {
                "severity": "ROUTINE",
                "reasoning": "Tenant references a past dispute; flagged for landlord review.",
            }
        }
    )
    assert result.severity == Severity.ROUTINE
    assert result.reasoning == ["Tenant references a past dispute; flagged for landlord review."]


# ---------------------------------------------------------------------------
# IntentResult round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_intent_result_round_trip() -> None:
    """IntentResult round-trips through model_dump → revalidate."""
    original = IntentResult(
        intent=Intent.maintenance,
        is_new_issue=True,
        summary="Burst pipe in bathroom",
    )
    dumped = original.model_dump()
    restored = IntentResult.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_intent_result_rejects_extra_fields() -> None:
    """IntentResult with extra='forbid' rejects unknown fields."""
    with pytest.raises(ValidationError):
        IntentResult(
            intent=Intent.admin,
            is_new_issue=False,
            summary="Parking question",
            bogus="nope",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_intent_result_summary_unbounded_but_nonempty() -> None:
    """summary maps to cases.title (unbounded text) — long is fine, empty is not."""
    # A long summary is accepted (no arbitrary cap that could reject valid output).
    long_summary = "x" * 200
    assert IntentResult(intent=Intent.other, is_new_issue=True, summary=long_summary).summary == (
        long_summary
    )
    # Empty summary is rejected (min_length=1).
    with pytest.raises(ValidationError):
        IntentResult(intent=Intent.other, is_new_issue=True, summary="")


@pytest.mark.unit
def test_intent_result_unwraps_wrapper_key() -> None:
    """IntentResult mirrors SeverityResult's wrapper-unwrap defensively --
    see that class's docstring "Robustness"."""
    result = IntentResult.model_validate(
        {
            "intent_result": {
                "intent": "admin",
                "is_new_issue": True,
                "summary": "Rent receipts for March to May",
            }
        }
    )
    assert result.intent == Intent.admin
    assert result.summary == "Rent receipts for March to May"


@pytest.mark.unit
def test_intent_result_nonsense_single_key_dict_still_fails() -> None:
    with pytest.raises(ValidationError):
        IntentResult.model_validate({"some_key": {"unrelated": "data"}})


# ---------------------------------------------------------------------------
# DraftResult round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_draft_result_round_trip_minimal() -> None:
    """Minimal DraftResult round-trips correctly."""
    original = DraftResult(body="Your issue has been received. I'll be in touch shortly.")
    dumped = original.model_dump()
    restored = DraftResult.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_draft_result_round_trip_with_refusals() -> None:
    """DraftResult with refusal templates round-trips correctly."""
    original = DraftResult(
        body="I can't discuss costs. Your issue has been received.",
        refusal_templates_used=[RefusalFlag.cost_compensation],
    )
    dumped = original.model_dump()
    restored = DraftResult.model_validate(dumped)
    assert restored == original
    assert RefusalFlag.cost_compensation in restored.refusal_templates_used


@pytest.mark.unit
def test_draft_result_rejects_empty_body() -> None:
    """DraftResult body must not be empty (min_length=1)."""
    with pytest.raises(ValidationError):
        DraftResult(body="")


@pytest.mark.unit
def test_draft_result_unwraps_wrapper_key() -> None:
    """DraftResult mirrors SeverityResult's wrapper-unwrap defensively --
    see that class's docstring "Robustness"."""
    result = DraftResult.model_validate(
        {"draft_result": {"body": "Thanks for letting me know, I'll follow up soon."}}
    )
    assert result.body == "Thanks for letting me know, I'll follow up soon."


@pytest.mark.unit
def test_draft_result_nonsense_single_key_dict_still_fails() -> None:
    with pytest.raises(ValidationError):
        DraftResult.model_validate({"some_key": {"unrelated": "data"}})


@pytest.mark.unit
def test_draft_result_rejects_extra_fields() -> None:
    """DraftResult rejects extra fields."""
    with pytest.raises(ValidationError):
        DraftResult(body="Hello", status="sent")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# PrefilterResult round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prefilter_result_round_trip_no_hit() -> None:
    """PrefilterResult with no matches round-trips correctly."""
    original = PrefilterResult(hard_hit=False)
    dumped = original.model_dump()
    restored = PrefilterResult.model_validate(dumped)
    assert restored == original
    assert restored.categories == []
    assert restored.soft_annotations == []
    assert restored.guards == []


@pytest.mark.unit
def test_prefilter_result_round_trip_hard_hit() -> None:
    """PrefilterResult with a hard hit round-trips correctly."""
    original = PrefilterResult(
        hard_hit=True,
        categories=["fire", "gas_co"],
        soft_annotations=[],
        guards=["smoke_detector_battery"],
    )
    dumped = original.model_dump()
    restored = PrefilterResult.model_validate(dumped)
    assert restored == original
    assert restored.hard_hit is True
    assert "fire" in restored.categories
    assert "smoke_detector_battery" in restored.guards


@pytest.mark.unit
def test_prefilter_result_round_trip_soft_only() -> None:
    """PrefilterResult with only soft annotations round-trips correctly."""
    original = PrefilterResult(
        hard_hit=False,
        soft_annotations=["no_heat", "freezing"],
    )
    dumped = original.model_dump()
    restored = PrefilterResult.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_prefilter_result_rejects_extra_fields() -> None:
    """PrefilterResult rejects extra fields."""
    with pytest.raises(ValidationError):
        PrefilterResult(hard_hit=False, unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# WeatherSnapshot round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_weather_snapshot_round_trip_defaults() -> None:
    """WeatherSnapshot with all defaults round-trips correctly."""
    original = WeatherSnapshot()
    dumped = original.model_dump()
    restored = WeatherSnapshot.model_validate(dumped)
    assert restored == original
    assert restored.current_temp_c is None
    assert restored.overnight_low_c is None
    assert restored.heat_warning is False


@pytest.mark.unit
def test_weather_snapshot_round_trip_full() -> None:
    """WeatherSnapshot with all fields set round-trips correctly."""
    original = WeatherSnapshot(
        current_temp_c=-12.5,
        overnight_low_c=-15.0,
        heat_warning=False,
    )
    dumped = original.model_dump()
    restored = WeatherSnapshot.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_weather_snapshot_heat_warning() -> None:
    """WeatherSnapshot with heat_warning=True round-trips correctly."""
    original = WeatherSnapshot(current_temp_c=38.0, overnight_low_c=30.0, heat_warning=True)
    dumped = original.model_dump()
    restored = WeatherSnapshot.model_validate(dumped)
    assert restored.heat_warning is True


@pytest.mark.unit
def test_weather_snapshot_rejects_extra_fields() -> None:
    """WeatherSnapshot rejects extra fields."""
    with pytest.raises(ValidationError):
        WeatherSnapshot(current_temp_c=20.0, feels_like_c=18.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# CaseContext round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_case_context_round_trip_empty() -> None:
    """CaseContext with all-None fields round-trips correctly."""
    original = CaseContext()
    dumped = original.model_dump()
    restored = CaseContext.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_case_context_round_trip_full() -> None:
    """CaseContext with all fields set round-trips correctly."""
    case_id = uuid.uuid4()
    property_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    landlord_id = uuid.uuid4()
    original = CaseContext(
        case_id=case_id,
        property_id=property_id,
        tenant_id=tenant_id,
        landlord_id=landlord_id,
        house_rules="No pets. Quiet hours 10pm–8am.",
        vulnerable_occupant=VulnerableOccupant.infant,
    )
    dumped = original.model_dump()
    restored = CaseContext.model_validate(dumped)
    assert restored == original
    assert restored.vulnerable_occupant is VulnerableOccupant.infant


@pytest.mark.unit
def test_case_context_rejects_extra_fields() -> None:
    """CaseContext rejects extra fields."""
    with pytest.raises(ValidationError):
        CaseContext(extra_flag=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ChannelMessage round-trip + validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_channel_message_round_trip_user() -> None:
    """ChannelMessage for an inbound tenant message round-trips correctly."""
    original = ChannelMessage(
        role="user",
        body="The heat is not working.",
        timestamp="2026-06-14T03:00:00Z",
    )
    dumped = original.model_dump()
    restored = ChannelMessage.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_channel_message_round_trip_assistant() -> None:
    """ChannelMessage for an outbound reply round-trips correctly."""
    original = ChannelMessage(
        role="assistant",
        body="I've received your report and will be in touch shortly.",
        timestamp="2026-06-14T03:01:00Z",
    )
    dumped = original.model_dump()
    restored = ChannelMessage.model_validate(dumped)
    assert restored == original


@pytest.mark.unit
def test_channel_message_rejects_invalid_role() -> None:
    """ChannelMessage rejects roles other than 'user' or 'assistant'."""
    with pytest.raises(ValidationError):
        ChannelMessage(role="system", body="ignored", timestamp="2026-06-14T03:00:00Z")


@pytest.mark.unit
def test_channel_message_rejects_extra_fields() -> None:
    """ChannelMessage rejects extra fields."""
    with pytest.raises(ValidationError):
        ChannelMessage(
            role="user",
            body="test",
            timestamp="2026-06-14T03:00:00Z",
            twilio_sid="SM123",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# AgentState construction and reasoning_log accumulation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_state_construction_representative_payload() -> None:
    """AgentState can be constructed with a representative full payload."""
    state: AgentState = {
        "case_context": CaseContext(
            case_id=uuid.uuid4(),
            property_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            landlord_id=uuid.uuid4(),
            house_rules="No pets.",
            vulnerable_occupant=VulnerableOccupant.elderly,
        ),
        "channel_history": [
            ChannelMessage(
                role="user",
                body="Heat is out.",
                timestamp="2026-06-14T02:00:00Z",
            ).model_dump()
        ],
        "prefilter": PrefilterResult(
            hard_hit=False,
            soft_annotations=["no_heat"],
        ),
        "weather": WeatherSnapshot(
            current_temp_c=-5.0,
            overnight_low_c=-12.0,
            heat_warning=False,
        ),
        "severity": SeverityResult(
            severity=Severity.EMERGENCY,
            rules_fired=["No heat when overnight low ≤ -10 °C"],
            modifier="vulnerable-occupant bump: elderly — URGENT → EMERGENCY",
            refusal_flags=[],
            reasoning=["Overnight low -12 °C triggers emergency threshold."],
        ),
        "draft": DraftResult(
            body="Please call 911 if you are in immediate danger. I have been notified."
        ),
        "reasoning_log": [
            "I matched this message to property matched via Twilio number",
            "I loaded the details for tenant loaded, vulnerable_occupant=elderly",
            "prefilter: no hard hit; soft_annotations=['no_heat']",
            "classify_severity: EMERGENCY — overnight low -12 °C ≤ -10 °C threshold",
            "vulnerable_occupant modifier applied: elderly raised URGENT → EMERGENCY",
            "draft_response: emergency safety draft composed",
        ],
    }

    assert state["severity"] is not None
    assert state["severity"].severity is Severity.EMERGENCY
    assert state["prefilter"] is not None
    assert state["prefilter"].hard_hit is False
    assert state["weather"] is not None
    assert state["weather"].overnight_low_c == -12.0
    assert state["draft"] is not None
    assert state["draft"].body.startswith("Please call 911")
    assert len(state["reasoning_log"]) == 6


@pytest.mark.unit
def test_agent_state_reasoning_log_accumulates() -> None:
    """reasoning_log accumulates entries as nodes run (mutable list semantics)."""
    state: AgentState = {
        "reasoning_log": [],
    }

    # Simulate nodes appending to the log one at a time.
    state["reasoning_log"].append("I matched this message to matched property abc")
    state["reasoning_log"].append("I loaded the details for context loaded")
    state["reasoning_log"].append("classify_severity: ROUTINE — minor dripping tap")

    assert len(state["reasoning_log"]) == 3
    assert state["reasoning_log"][0] == "I matched this message to matched property abc"
    assert state["reasoning_log"][2] == "classify_severity: ROUTINE — minor dripping tap"


@pytest.mark.unit
def test_agent_state_partial_construction() -> None:
    """AgentState can be constructed with only some fields (total=False)."""
    state: AgentState = {
        "case_context": CaseContext(),
        "reasoning_log": ["I matched this message to matched property xyz"],
    }
    # Fields not yet populated should be absent (not KeyError on TypedDict).
    assert state.get("severity") is None
    assert state.get("draft") is None
    assert state.get("prefilter") is None
    assert len(state["reasoning_log"]) == 1
