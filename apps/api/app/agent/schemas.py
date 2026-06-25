"""Shared Pydantic models and enums for the Stoop agent.

This is the single source of truth for the agent's data vocabulary.  All
agent nodes, tool definitions (#29), and the prefilter (#107) import their
types from here — never define parallel schemas elsewhere.

Design rules:
- Every model uses ``model_config = ConfigDict(extra="forbid")`` (strict).
- Enums values MUST match external contracts:
    Severity   → rubric v1.0 (EMERGENCY / URGENT / ROUTINE, uppercase)
    RefusalFlag → keys in ``prompts/v1.py`` REFUSAL_TEMPLATES (snake_case)
    VulnerableOccupant → tenants.vulnerable_occupant CHECK in schema-v1.md
- No I/O, no DB, no Anthropic calls here — pure data shapes.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    """Severity levels — match the rubric v1.0 output tokens exactly.

    The rubric (``app/agent/rubric.py``) and the classify_severity prompt
    both emit these strings verbatim.  Changing any value here is a rubric
    version bump + full eval run.

    NOTE on DB persistence (schema-v1.md rule #6): the enum VALUES are
    uppercase to match the frozen rubric's output tokens, but the
    ``cases.severity`` / ``trust_metrics.severity`` columns use a LOWERCASE
    CHECK constraint (``CHECK (severity IN ('emergency','urgent','routine'))``).
    Persist via :pyattr:`db_value`, never ``.value`` directly, or Postgres
    rejects the write.
    """

    EMERGENCY = "EMERGENCY"
    URGENT = "URGENT"
    ROUTINE = "ROUTINE"

    @property
    def db_value(self) -> str:
        """The lowercase form written to schema-v1 severity columns."""
        return self.value.lower()


class RefusalFlag(StrEnum):
    """Refusal topics the agent must never engage with substantively.

    Values are IDENTICAL to the keys in ``prompts/v1.py`` REFUSAL_TEMPLATES
    so that ``REFUSAL_TEMPLATES[flag.value]`` always resolves without a
    mapping step.  Changing any key here requires updating v1.py in the same
    commit (or, if v1.py is frozen, creating v2.py — see CLAUDE.md).
    """

    access_codes = "access_codes"
    legal_rent_ltb = "legal_rent_ltb"
    cost_compensation = "cost_compensation"
    other_tenants = "other_tenants"
    impersonation = "impersonation"


class VulnerableOccupant(StrEnum):
    """Vulnerable-occupant categories from tenants.vulnerable_occupant CHECK.

    Values match the Postgres CHECK constraint in schema-v1.md exactly:
    ``CHECK (vulnerable_occupant IN ('infant','elderly','medical_device'))``.
    """

    infant = "infant"
    elderly = "elderly"
    medical_device = "medical_device"


class Intent(StrEnum):
    """Intent categories matching ``cases.intent`` in schema-v1.md.

    ``cases.intent`` stores ``maintenance|admin|question|other``.  The
    conversation-model.md message-routing step uses these four categories.
    ``chitchat`` is handled by the routing logic (no case opened) and is not
    stored in the cases table, so it is deliberately excluded here.
    """

    maintenance = "maintenance"
    admin = "admin"
    question = "question"
    other = "other"


# ---------------------------------------------------------------------------
# Core result models
# ---------------------------------------------------------------------------


class SeverityResult(BaseModel):
    """Output of the classify_severity node.

    ``rules_fired`` lists the specific rubric rules that triggered the
    classification (e.g. "No heat when outdoor temp ≤ -10 °C").  This list
    is displayed on the landlord's approval card via ``reasoning_log``.

    ``modifier`` is ``None`` when no modifier was applied; otherwise a
    human-readable string describing the modifier (e.g.
    "vulnerable-occupant bump: infant — raised from URGENT to EMERGENCY").

    ``refusal_flags`` lists every refusal topic detected in the message.
    An empty list means no refusal topics were present.

    ``reasoning`` holds per-issue one-sentence explanations (one entry per
    distinct issue found in a multi-issue message).  The node also appends
    a summary line to ``AgentState.reasoning_log`` for the approval card.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    rules_fired: list[str] = Field(default_factory=list)
    modifier: str | None = None
    refusal_flags: list[RefusalFlag] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)


class IntentResult(BaseModel):
    """Output of the classify_intent node.

    ``intent`` maps to ``cases.intent`` in schema-v1.md.  The four values
    are defined by the ``Intent`` enum above.

    ``is_new_issue`` signals whether the message starts a new case (True) or
    continues / references an existing open case (False).  The
    ``identify_case`` node uses this to decide whether to open a new case or
    attach the message to an existing one.

    ``summary`` is a short agent-written title for the case (maps to
    ``cases.title`` — plain ``text`` in schema-v1.md, no length limit, so we
    impose none here; keeping it short for the approval-card header is a
    prompt instruction, not a hard validation cap that could reject a valid
    classification).
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    is_new_issue: bool
    summary: str = Field(min_length=1)


class DraftResult(BaseModel):
    """Output of the draft_response node.

    ``body`` is the full SMS reply text the agent drafted in the landlord's
    voice.  It is stored in ``drafts.body`` and shown for approval.

    ``refusal_templates_used`` records which canned refusal-deferral phrases
    were injected into the draft body (from ``prompts/v1.py``
    REFUSAL_TEMPLATES).  Empty when the message had no refusal topics.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    refusal_templates_used: list[RefusalFlag] = Field(default_factory=list)


class PrefilterResult(BaseModel):
    """Output shape of the Tier-0 deterministic keyword filter.

    Defined here so graph nodes and schema consumers can type-check against
    it.  The ``check()`` function that PRODUCES this result is implemented
    in ``app/agent/prefilter.py`` (#107) — not here.

    ``hard_hit`` is True when at least one HARD trigger category matched.
    A hard hit fires the emergency protocol immediately, without waiting for
    LLM classification.

    ``categories`` lists the HARD trigger categories that fired (e.g.
    ``["fire", "gas_co"]``).  Empty when ``hard_hit`` is False.

    ``soft_annotations`` lists SOFT keyword matches (e.g.
    ``["no_heat", "freezing"]``) that did not fire the protocol alone but are
    attached as hints for the classifier and degrade-mode logic.

    ``guards`` lists guard patterns that matched but were overridden by a
    trigger (e.g. ``["smoke_detector_battery"]``).  Recorded for review so the
    guard list can be refined without silently hiding information.

    Field shape is documented in docs/02-product/emergency-prefilter.md
    ("PrefilterResult shape"); #107 implements ``check()`` against this model.
    """

    model_config = ConfigDict(extra="forbid")

    hard_hit: bool
    categories: list[str] = Field(default_factory=list)
    soft_annotations: list[str] = Field(default_factory=list)
    guards: list[str] = Field(default_factory=list)


class WeatherSnapshot(BaseModel):
    """Current and forecast weather data for the property's location.

    Feeds the rubric's temperature-dependent rules:
    - ``current_temp_c`` and ``overnight_low_c`` drive the -10 °C EMERGENCY
      threshold for no-heat cases.
    - ``heat_warning`` drives the "no AC during an official heat warning" →
      URGENT rule.

    All fields are optional so the model can be constructed with partial data
    when the weather API is unavailable; classify_severity must treat ``None``
    temperatures conservatively (escalate if unsure — bias rule).
    """

    model_config = ConfigDict(extra="forbid")

    current_temp_c: float | None = None
    overnight_low_c: float | None = None
    heat_warning: bool = False


class CaseContext(BaseModel):
    """Lightweight case/tenant/property identifiers the graph carries.

    Populated by the ``identify_property`` and ``load_context`` nodes from
    the database; downstream nodes read from here rather than re-querying.
    All fields are optional because the graph may be constructed before
    routing is complete.

    ``house_rules`` is the verbatim text from ``properties.house_rules``
    injected into the draft prompt.

    ``vulnerable_occupant`` comes from ``tenants.vulnerable_occupant`` and
    triggers the VULNERABLE-OCCUPANT MODIFIER in ``classify_severity``.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: UUID | None = None
    property_id: UUID | None = None
    tenant_id: UUID | None = None
    landlord_id: UUID | None = None
    house_rules: str | None = None
    vulnerable_occupant: VulnerableOccupant | None = None


class ChannelMessage(BaseModel):
    """A single message in the channel history slice passed to the agent.

    This is not a DB model — it is a lightweight slice of recent messages
    extracted from the ``messages`` table for context loading.  ``role``
    follows the convention used in LLM message lists: ``"user"`` for inbound
    tenant messages and ``"assistant"`` for outbound replies.

    ``body`` is the message text.  Per project rules, this field MUST NOT be
    logged or included in Sentry events — it is PII.

    ``timestamp`` is an ISO-8601 UTC string (``created_at`` from the DB row).
    """

    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(user|assistant)$")
    body: str
    timestamp: str
