"""Anthropic tool definitions for the Stoop agent.

Single source of truth: each tool's input shape is a Pydantic model; the
Anthropic-API tool dict (``{"name", "description", "input_schema"}``) is
generated from ``Model.model_json_schema()``.  Never handwrite the JSON
Schema — always derive it from the model.

Design decisions:
- ``classify_severity``, ``classify_intent``, and ``draft_message`` reuse
  ``SeverityResult``, ``IntentResult``, and ``DraftResult`` from
  ``app.agent.schemas`` directly.  Their field shapes already match what the
  Anthropic model must return for each tool, so there is no reason to define
  parallel classes.
- ``identify_case`` needs a shape that does not exist in schemas.py (it
  carries ``is_new_case``, ``matched_case_id``, and a ``summary``), so a new
  ``IdentifyCaseArgs`` model is defined here.

No anthropic SDK import — the SDK is not yet a dependency (lands with the
LLM graph nodes in #30–#34).  The dicts produced here are exactly what
``anthropic.types.ToolParam`` expects; callers in those issues will pass them
directly.

Rule compliance:
- Enum values imported from schemas.py — never re-declared here (rule #4/#6).
- No I/O, no DB, no Anthropic calls — pure data + schema generation.
- No feature flags (CLAUDE.md agent rules).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.agent.schemas import (
    DraftResult,
    IntentResult,
    SeverityResult,
)

# ---------------------------------------------------------------------------
# Tool input models
# ---------------------------------------------------------------------------

# classify_severity: reuse SeverityResult (shape is identical to what the
# model must return).  See schemas.py for field docs.
ClassifySeverityArgs = SeverityResult

# classify_intent: reuse IntentResult (shape is identical).
ClassifyIntentArgs = IntentResult

# draft_message: reuse DraftResult (shape is identical).
DraftMessageArgs = DraftResult


class IdentifyCaseArgs(BaseModel):
    """Input the model must return for the ``identify_case`` tool.

    The ``identify_case`` step (conversation-model.md §"Message routing")
    decides whether an inbound message opens a new case or attaches to an
    existing open case.

    ``is_new_case`` — True when the message describes a new issue that should
    open a fresh case record.  False when it continues, updates, or follows up
    on an existing open case.

    ``matched_case_id`` — UUID of the existing open case this message belongs
    to, or None when ``is_new_case`` is True.  The graph node uses this to
    set ``messages.case_id`` and to decide whether to reopen a recently
    resolved case (30-day window, per conversation-model.md).

    ``summary`` — A one-sentence description of the issue or update.  Written
    by the model for the landlord's approval card.  Maps to ``cases.title``
    (unbounded ``text`` in schema-v1.md) on new cases; appended to
    ``reasoning_log`` on existing cases.
    """

    model_config = ConfigDict(extra="forbid")

    is_new_case: bool
    matched_case_id: UUID | None = None
    summary: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Anthropic tool dicts (name / description / input_schema)
#
# Descriptions are written to guide the LLM, not the developer.  They tell
# the model WHEN to call the tool and what each tool expects in return.
# ---------------------------------------------------------------------------

CLASSIFY_SEVERITY_TOOL: dict[str, Any] = {
    "name": "classify_severity",
    "description": (
        "Call this tool to classify the severity of the tenant's maintenance issue "
        "according to the Stoop rubric v1.0.  Return the highest applicable severity "
        "(EMERGENCY > URGENT > ROUTINE), the specific rubric rules that fired, any "
        "vulnerable-occupant or temperature modifier that was applied, any refusal "
        "topics detected, and a per-issue reasoning sentence for the landlord's "
        "approval card."
    ),
    "input_schema": ClassifySeverityArgs.model_json_schema(),
}

CLASSIFY_INTENT_TOOL: dict[str, Any] = {
    "name": "classify_intent",
    "description": (
        "Call this tool to classify the intent of the tenant's message and decide "
        "whether it represents a new issue.  Return the intent category "
        "(maintenance | admin | question | other), whether this is a new issue or a "
        "continuation of an existing one, and a short one-sentence summary of the "
        "issue for the approval card."
    ),
    "input_schema": ClassifyIntentArgs.model_json_schema(),
}

# NB: the Anthropic *tool* is named "draft_message" (per issue #29); the graph
# *node* that invokes it is named "draft_response" (architecture.md §4,
# apps/api/CLAUDE.md). Different layers, intentionally — don't rename either to
# match the other (#30–#34 reference the tool by the string "draft_message").
DRAFT_MESSAGE_TOOL: dict[str, Any] = {
    "name": "draft_message",
    "description": (
        "Call this tool to produce a drafted SMS reply to the tenant in the "
        "landlord's voice.  The body must be concise, respectful, and appropriate "
        "to the issue's severity.  If any refusal topics were detected, incorporate "
        "the corresponding refusal deferral phrases verbatim and list them in "
        "refusal_templates_used."
    ),
    "input_schema": DraftMessageArgs.model_json_schema(),
}

IDENTIFY_CASE_TOOL: dict[str, Any] = {
    "name": "identify_case",
    "description": (
        "Call this tool after classifying intent to decide whether the inbound "
        "message opens a new case or belongs to an existing open case.  Set "
        "is_new_case=True and matched_case_id=None for a new issue; set "
        "is_new_case=False and matched_case_id to the UUID of the existing case "
        "this message continues.  Provide a one-sentence summary for the landlord."
    ),
    "input_schema": IdentifyCaseArgs.model_json_schema(),
}

# Ordered list the graph nodes can pass directly to the Anthropic API's
# ``tools`` parameter.
ALL_TOOLS: list[dict[str, Any]] = [
    CLASSIFY_SEVERITY_TOOL,
    CLASSIFY_INTENT_TOOL,
    DRAFT_MESSAGE_TOOL,
    IDENTIFY_CASE_TOOL,
]
