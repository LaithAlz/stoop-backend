"""Unit tests for app/agent/tools.py.

Covers:
- Each tool dict has the required keys (name, description, input_schema).
- input_schema top-level type is "object" (Anthropic requirement).
- Severity enum values in the generated JSON schema are EMERGENCY/URGENT/ROUTINE.
- Valid payloads parse through each tool's input model.
- Invalid payloads (bad enum, missing required field, extra field) raise ValidationError.
- ALL_TOOLS contains exactly the four expected tool dicts.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.tools import (
    ALL_TOOLS,
    CLASSIFY_INTENT_TOOL,
    CLASSIFY_SEVERITY_TOOL,
    DRAFT_MESSAGE_TOOL,
    IDENTIFY_CASE_TOOL,
    ClassifyIntentArgs,
    ClassifySeverityArgs,
    DraftMessageArgs,
    IdentifyCaseArgs,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_ALL_TOOL_DICTS: list[dict[str, Any]] = [
    CLASSIFY_SEVERITY_TOOL,
    CLASSIFY_INTENT_TOOL,
    DRAFT_MESSAGE_TOOL,
    IDENTIFY_CASE_TOOL,
]


# ---------------------------------------------------------------------------
# Tool dict structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("tool", _ALL_TOOL_DICTS)
def test_tool_has_required_keys(tool: dict[str, Any]) -> None:
    """Every tool dict must have name, description, and input_schema."""
    assert "name" in tool, f"Tool missing 'name': {tool}"
    assert "description" in tool, f"Tool missing 'description': {tool}"
    assert "input_schema" in tool, f"Tool missing 'input_schema': {tool}"


@pytest.mark.unit
@pytest.mark.parametrize("tool", _ALL_TOOL_DICTS)
def test_tool_name_is_string(tool: dict[str, Any]) -> None:
    """Tool name must be a non-empty string."""
    assert isinstance(tool["name"], str)
    assert len(tool["name"]) > 0


@pytest.mark.unit
@pytest.mark.parametrize("tool", _ALL_TOOL_DICTS)
def test_tool_description_is_string(tool: dict[str, Any]) -> None:
    """Tool description must be a non-empty string."""
    assert isinstance(tool["description"], str)
    assert len(tool["description"]) > 0


@pytest.mark.unit
@pytest.mark.parametrize("tool", _ALL_TOOL_DICTS)
def test_input_schema_top_level_type_is_object(tool: dict[str, Any]) -> None:
    """Anthropic requires input_schema to be a JSON Schema object with type='object'."""
    schema: dict[str, Any] = tool["input_schema"]
    assert schema.get("type") == "object", (
        f"Tool '{tool['name']}' input_schema top-level type must be 'object', "
        f"got: {schema.get('type')!r}"
    )


@pytest.mark.unit
def test_tool_names_match_expected() -> None:
    """Tool names must be the four expected snake_case identifiers."""
    names = {t["name"] for t in _ALL_TOOL_DICTS}
    assert names == {
        "classify_severity",
        "classify_intent",
        "draft_message",
        "identify_case",
    }


@pytest.mark.unit
def test_all_tools_list_contains_four_tools() -> None:
    """ALL_TOOLS must contain exactly the four tool dicts."""
    assert len(ALL_TOOLS) == 4
    all_names = {t["name"] for t in ALL_TOOLS}
    assert all_names == {
        "classify_severity",
        "classify_intent",
        "draft_message",
        "identify_case",
    }


# ---------------------------------------------------------------------------
# Severity enum values in the generated JSON schema (rubric v1.0 compliance)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_severity_schema_enum_matches_rubric_v1() -> None:
    """The severity enum in the generated JSON schema must be exactly
    EMERGENCY/URGENT/ROUTINE — matching rubric v1.0 output tokens.

    Pydantic may emit the enum values via $defs/$ref; we resolve them.
    """
    schema: dict[str, Any] = CLASSIFY_SEVERITY_TOOL["input_schema"]

    # Locate the severity enum — either inline in properties or via $defs/$ref.
    severity_schema: dict[str, Any] | None = None
    props = schema.get("properties", {})
    sev_prop = props.get("severity", {})

    if "$ref" in sev_prop:
        # Resolve the $ref against $defs (standard Pydantic v2 output).
        ref: str = sev_prop["$ref"]
        # ref format: "#/$defs/Severity"
        def_name = ref.split("/")[-1]
        defs: dict[str, Any] = schema.get("$defs", {})
        severity_schema = defs.get(def_name)
    else:
        severity_schema = sev_prop

    assert severity_schema is not None, (
        "Could not locate Severity definition in the generated JSON schema"
    )
    enum_values: list[Any] = severity_schema.get("enum", [])
    assert set(enum_values) == {"EMERGENCY", "URGENT", "ROUTINE"}, (
        f"Severity enum in JSON schema does not match rubric v1.0. Got: {enum_values}"
    )


# ---------------------------------------------------------------------------
# classify_severity — valid payloads parse; invalid ones raise ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_severity_valid_minimal() -> None:
    """Minimal valid payload parses through ClassifySeverityArgs."""
    result = ClassifySeverityArgs.model_validate({"severity": "ROUTINE"})
    assert result.severity.value == "ROUTINE"
    assert result.rules_fired == []
    assert result.modifier is None
    assert result.refusal_flags == []
    assert result.reasoning == []


@pytest.mark.unit
def test_classify_severity_valid_full() -> None:
    """Full valid payload with all optional fields parses correctly."""
    payload: dict[str, Any] = {
        "severity": "EMERGENCY",
        "rules_fired": ["burst pipe — active uncontained water"],
        "modifier": "vulnerable-occupant bump: infant — URGENT → EMERGENCY",
        "refusal_flags": ["access_codes"],
        "reasoning": ["Active burst pipe is a life-safety hazard."],
    }
    result = ClassifySeverityArgs.model_validate(payload)
    assert result.severity.value == "EMERGENCY"
    assert result.rules_fired == ["burst pipe — active uncontained water"]
    assert result.modifier is not None
    assert "infant" in result.modifier


@pytest.mark.unit
def test_classify_severity_rejects_bad_severity_enum() -> None:
    """An unrecognised severity value must raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassifySeverityArgs.model_validate({"severity": "CRITICAL"})


@pytest.mark.unit
def test_classify_severity_rejects_missing_required_field() -> None:
    """Omitting the required 'severity' field must raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassifySeverityArgs.model_validate({"rules_fired": ["some rule"]})


@pytest.mark.unit
def test_classify_severity_rejects_extra_field() -> None:
    """extra='forbid' must reject unrecognised fields."""
    with pytest.raises(ValidationError):
        ClassifySeverityArgs.model_validate({"severity": "URGENT", "unexpected_field": "bad"})


@pytest.mark.unit
def test_classify_severity_rejects_bad_refusal_flag() -> None:
    """An unrecognised refusal flag value must raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassifySeverityArgs.model_validate(
            {"severity": "ROUTINE", "refusal_flags": ["invalid_flag"]}
        )


# ---------------------------------------------------------------------------
# classify_intent — valid payloads parse; invalid ones raise ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classify_intent_valid() -> None:
    """Valid payload parses through ClassifyIntentArgs."""
    result = ClassifyIntentArgs.model_validate(
        {"intent": "maintenance", "is_new_issue": True, "summary": "Burst pipe in bathroom"}
    )
    assert result.intent.value == "maintenance"
    assert result.is_new_issue is True
    assert result.summary == "Burst pipe in bathroom"


@pytest.mark.unit
def test_classify_intent_rejects_bad_intent_enum() -> None:
    """An unrecognised intent value must raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassifyIntentArgs.model_validate(
            {"intent": "emergency", "is_new_issue": True, "summary": "Fire"}
        )


@pytest.mark.unit
def test_classify_intent_rejects_missing_required_field() -> None:
    """Omitting 'intent' must raise ValidationError."""
    with pytest.raises(ValidationError):
        ClassifyIntentArgs.model_validate({"is_new_issue": True, "summary": "Something"})


@pytest.mark.unit
def test_classify_intent_rejects_extra_field() -> None:
    """extra='forbid' must reject unrecognised fields."""
    with pytest.raises(ValidationError):
        ClassifyIntentArgs.model_validate(
            {
                "intent": "admin",
                "is_new_issue": False,
                "summary": "Parking question",
                "bogus": "value",
            }
        )


@pytest.mark.unit
def test_classify_intent_rejects_empty_summary() -> None:
    """An empty summary must raise ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        ClassifyIntentArgs.model_validate(
            {"intent": "question", "is_new_issue": True, "summary": ""}
        )


# ---------------------------------------------------------------------------
# draft_message — valid payloads parse; invalid ones raise ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_draft_message_valid_minimal() -> None:
    """Minimal valid payload parses through DraftMessageArgs."""
    result = DraftMessageArgs.model_validate(
        {"body": "Your issue has been received and I'll follow up soon."}
    )
    assert result.body.startswith("Your issue")
    assert result.refusal_templates_used == []


@pytest.mark.unit
def test_draft_message_valid_with_refusal() -> None:
    """Payload with refusal templates parses correctly."""
    result = DraftMessageArgs.model_validate(
        {
            "body": "I can't discuss repair costs. Your landlord will be in touch.",
            "refusal_templates_used": ["cost_compensation"],
        }
    )
    assert result.refusal_templates_used[0].value == "cost_compensation"


@pytest.mark.unit
def test_draft_message_rejects_empty_body() -> None:
    """An empty body must raise ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        DraftMessageArgs.model_validate({"body": ""})


@pytest.mark.unit
def test_draft_message_rejects_missing_body() -> None:
    """Omitting 'body' must raise ValidationError."""
    with pytest.raises(ValidationError):
        DraftMessageArgs.model_validate({"refusal_templates_used": []})


@pytest.mark.unit
def test_draft_message_rejects_extra_field() -> None:
    """extra='forbid' must reject unrecognised fields."""
    with pytest.raises(ValidationError):
        DraftMessageArgs.model_validate(
            {"body": "Hello", "refusal_templates_used": [], "status": "sent"}
        )


@pytest.mark.unit
def test_draft_message_rejects_bad_refusal_flag() -> None:
    """An unrecognised refusal flag value must raise ValidationError."""
    with pytest.raises(ValidationError):
        DraftMessageArgs.model_validate(
            {"body": "Reply text", "refusal_templates_used": ["unknown_flag"]}
        )


# ---------------------------------------------------------------------------
# identify_case — valid payloads parse; invalid ones raise ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_identify_case_valid_new_case() -> None:
    """Valid payload for a new case (no matched_case_id) parses correctly."""
    result = IdentifyCaseArgs.model_validate(
        {"is_new_case": True, "matched_case_id": None, "summary": "Tenant reports dripping tap"}
    )
    assert result.is_new_case is True
    assert result.matched_case_id is None
    assert result.summary == "Tenant reports dripping tap"


@pytest.mark.unit
def test_identify_case_valid_existing_case() -> None:
    """Valid payload attaching to an existing case parses correctly."""
    case_id = uuid.uuid4()
    result = IdentifyCaseArgs.model_validate(
        {
            "is_new_case": False,
            "matched_case_id": str(case_id),
            "summary": "Follow-up on the dripping tap reported earlier",
        }
    )
    assert result.is_new_case is False
    assert result.matched_case_id == case_id


@pytest.mark.unit
def test_identify_case_valid_matched_case_id_defaults_none() -> None:
    """matched_case_id defaults to None when omitted."""
    result = IdentifyCaseArgs.model_validate(
        {"is_new_case": True, "summary": "New leak under the sink"}
    )
    assert result.matched_case_id is None


@pytest.mark.unit
def test_identify_case_rejects_missing_is_new_case() -> None:
    """Omitting 'is_new_case' must raise ValidationError."""
    with pytest.raises(ValidationError):
        IdentifyCaseArgs.model_validate({"summary": "Something"})


@pytest.mark.unit
def test_identify_case_rejects_missing_summary() -> None:
    """Omitting 'summary' must raise ValidationError."""
    with pytest.raises(ValidationError):
        IdentifyCaseArgs.model_validate({"is_new_case": True})


@pytest.mark.unit
def test_identify_case_rejects_empty_summary() -> None:
    """An empty summary must raise ValidationError (min_length=1)."""
    with pytest.raises(ValidationError):
        IdentifyCaseArgs.model_validate({"is_new_case": True, "summary": ""})


@pytest.mark.unit
def test_identify_case_rejects_extra_field() -> None:
    """extra='forbid' must reject unrecognised fields."""
    with pytest.raises(ValidationError):
        IdentifyCaseArgs.model_validate(
            {"is_new_case": True, "summary": "New issue", "landlord_approved": True}
        )


@pytest.mark.unit
def test_identify_case_rejects_bad_uuid() -> None:
    """An invalid UUID string for matched_case_id must raise ValidationError."""
    with pytest.raises(ValidationError):
        IdentifyCaseArgs.model_validate(
            {"is_new_case": False, "matched_case_id": "not-a-uuid", "summary": "Dripping tap"}
        )
