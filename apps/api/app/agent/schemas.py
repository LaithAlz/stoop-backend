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
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


def _unwrap_single_key_wrapper(data: object, known_fields: set[str]) -> object:
    """Shared logic behind ``SeverityResult``/``IntentResult``/
    ``DraftResult``'s model-level "unwrap a single-key wrapper dict"
    before-validator.

    Paid eval gate finding, 2026-07-05: the model sometimes nests its tool
    input under an extra wrapper key instead of returning the flat shape
    the tool schema asks for -- observed live: ``{"severity_result": {...
    actual fields ...}}`` and ``{"severity_input": {...}}``. Same
    "model-output-shape variance" class as ``SeverityResult``'s
    ``_coerce_singular_to_list`` (a bare string instead of a one-item
    list) -- both are the model failing to hit the EXACT forced-tool
    shape while still clearly containing the intended answer.

    Unwraps ONLY when ALL of the following hold, so a genuinely wrong
    payload still fails validation normally rather than being silently
    "rescued" into some other shape:

    - *data* is a dict with EXACTLY one key (a flat payload that
      legitimately has every-other-field-at-default and only one field
      SET still has that field as a TOP-LEVEL key, e.g.
      ``{"severity": "ROUTINE"}`` -- that key IS a known field name, so
      the very next check below already leaves it untouched);
    - that one key is NOT itself a recognized field name of the target
      model (a real flat payload's single set field always passes this
      check without being touched -- see above);
    - the value under that key is ITSELF a dict;
    - that inner dict contains AT LEAST ONE recognized field name (e.g.
      ``'severity'``) -- a single-key dict whose inner dict has NO
      recognized field names is left completely alone; it is a genuinely
      malformed payload, not a wrapper, and must still raise the normal
      ``missing``/``extra_forbidden`` validation errors.

    When all hold, returns the INNER dict (the unwrapped payload);
    otherwise returns *data* completely unchanged.
    """
    if not isinstance(data, dict) or len(data) != 1:
        return data
    ((outer_key, inner_value),) = data.items()
    if outer_key in known_fields:
        return data
    if not isinstance(inner_value, dict):
        return data
    if not (set(inner_value) & known_fields):
        return data
    return inner_value


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

    Robustness (paid eval gate finding, 2026-07-05): the real gate's F1
    scenario got ``reasoning`` back as a bare string (not a list) once --
    the model, faced with an INCONCLUSIVE-shaped message, emitted a single
    unwrapped sentence instead of a one-item list. ``rules_fired`` and
    ``refusal_flags`` carry the identical "the model found exactly one
    thing and unwrapped it" risk (nothing about their JSON-schema shape
    stops a model from doing the same), so all three list-typed fields
    share the same before-validator (:func:`_coerce_singular_to_list`)
    coercing a bare string into a single-item list before Pydantic's own
    list/enum validation runs -- a real list (or ``None``, for fields that
    allow it) passes through unchanged.

    A LATER gate run (same day) surfaced a second, model-level variance:
    the whole payload nested under an extra wrapper key (``{"severity_
    result": {...}}`` / ``{"severity_input": {...}}``) instead of the flat
    shape. ``_unwrap_wrapper`` (below) unwraps that BEFORE the field-level
    coercion above ever runs -- see :func:`_unwrap_single_key_wrapper` for
    the exact, deliberately-narrow conditions (a genuinely wrong payload
    still fails validation normally).

    Gate 8 (2026-07-06) surfaced a THIRD variance on the e4 injection
    scenario: ``refusal_flags`` came back as a per-flag boolean dict
    (``{"access_codes": false, ...}``) instead of a list of fired flags,
    alongside an invented boolean field
    ``vulnerable_occupant_modifier_applied``. Two more deliberately-narrow
    coercions absorb exactly those shapes: ``_coerce_flag_dict_to_list``
    (a str->bool dict becomes the list of true keys) and
    ``_absorb_boolean_modifier_variant`` (the invented key is removed;
    ``True`` is TRANSLATED into a ``modifier`` string rather than dropped,
    because silently discarding a vulnerable-occupant signal would be a
    de-escalation -- rule: never lose an escalation signal to shape
    normalization).
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    rules_fired: list[str] = Field(default_factory=list)
    modifier: str | None = None
    refusal_flags: list[RefusalFlag] = Field(default_factory=list)
    reasoning: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _unwrap_wrapper(cls, data: object) -> object:
        """Unwrap a single-key wrapper FIRST (see class docstring
        "Robustness" / :func:`_unwrap_single_key_wrapper`), THEN absorb the
        gate-8 boolean-modifier variant on the unwrapped payload. One
        validator with explicit sequencing -- Pydantic executes multiple
        ``mode="before"`` model validators in REVERSE definition order,
        which silently broke the two-validator version when the variances
        composed (wrapper outside, invented key inside)."""
        data = _unwrap_single_key_wrapper(data, set(cls.model_fields))
        # Gate-8 variance (see class docstring): the model invented
        # ``vulnerable_occupant_modifier_applied: bool``. Absorb EXACTLY a
        # bool under that key: False is dropped (identical meaning to
        # absence); True becomes a ``modifier`` string when none was given,
        # so the escalation signal survives. A non-bool value is left in
        # place and fails ``extra="forbid"`` normally.
        if isinstance(data, dict):
            flag = data.get("vulnerable_occupant_modifier_applied")
            if isinstance(flag, bool):
                data = dict(data)
                del data["vulnerable_occupant_modifier_applied"]
                if flag and not data.get("modifier"):
                    data["modifier"] = "vulnerable occupant present (model emitted boolean variant)"
        return data

    @field_validator("refusal_flags", mode="before")
    @classmethod
    def _coerce_flag_dict_to_list(cls, value: object) -> object:
        """Gate-8 variance (see class docstring): a per-flag boolean dict
        in place of the list of fired flags. Coerce EXACTLY a str->bool
        dict into the list of keys whose value is true; any other dict
        shape passes through and fails list validation normally."""
        if (
            isinstance(value, dict)
            and all(isinstance(k, str) for k in value)
            and all(isinstance(v, bool) for v in value.values())
        ):
            return [k for k, v in value.items() if v]
        return value

    @field_validator("rules_fired", "refusal_flags", "reasoning", mode="before")
    @classmethod
    def _coerce_singular_to_list(cls, value: object) -> object:
        """A bare string in place of a single-item list -- see class
        docstring "Robustness". Anything else (a real list, ``None``, ...)
        passes through unchanged for Pydantic's own validation."""
        if isinstance(value, str):
            return [value]
        return value


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

    Robustness (paid eval gate finding, 2026-07-05): ``classify_intent``
    shares ``call_tool_forced``'s single-flat-object tool shape with
    ``classify_severity``, so it carries the identical "model nests its
    answer under an extra wrapper key" risk ``SeverityResult`` observed
    live (see that class's own docstring) — mirrored here defensively,
    even though this exact model hasn't shown the failure yet.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    is_new_issue: bool
    summary: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _unwrap_wrapper(cls, data: object) -> object:
        """See class docstring "Robustness" / :func:`_unwrap_single_key_wrapper`."""
        return _unwrap_single_key_wrapper(data, set(cls.model_fields))


class DraftResult(BaseModel):
    """Output of the draft_response node.

    ``body`` is the full SMS reply text the agent drafted in the landlord's
    voice.  It is stored in ``drafts.body`` and shown for approval.

    ``refusal_templates_used`` records which canned refusal-deferral phrases
    were injected into the draft body (from ``prompts/v1.py``
    REFUSAL_TEMPLATES).  Empty when the message had no refusal topics.

    Robustness (paid eval gate finding, 2026-07-05): same wrapper-key
    mirror as ``IntentResult`` above — see ``SeverityResult``'s docstring
    for the finding this defends against.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    refusal_templates_used: list[RefusalFlag] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _unwrap_wrapper(cls, data: object) -> object:
        """See class docstring "Robustness" / :func:`_unwrap_single_key_wrapper`."""
        return _unwrap_single_key_wrapper(data, set(cls.model_fields))


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

    Fields added for #30 (``load_context``) — every one is a verbatim
    projection of a schema-v1.md column/jsonb blob, never re-shaped:

    ``quiet_hours`` / ``heating_season`` / ``backup_contact`` — the
    ``properties`` jsonb columns of the same name (``quiet_hours``/
    ``heating_season`` carry NOT NULL defaults at the DB level; ``None``
    here just means "not loaded yet", not "the property has none").

    ``voice_profile`` — ``landlords.voice_profile`` (nullable jsonb:
    ``{tone: text, samples: text[]}``), injected into ``draft_response``'s
    prompt so replies sound like the landlord, not a generic bot.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: UUID | None = None
    property_id: UUID | None = None
    tenant_id: UUID | None = None
    landlord_id: UUID | None = None
    house_rules: str | None = None
    vulnerable_occupant: VulnerableOccupant | None = None
    quiet_hours: dict[str, Any] | None = None
    heating_season: dict[str, Any] | None = None
    backup_contact: dict[str, Any] | None = None
    voice_profile: dict[str, Any] | None = None


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


class OpenCaseSummary(BaseModel):
    """A lightweight summary of one of the tenant's OPEN cases (#30/#110).

    Not a DB model — a read-side projection of a ``cases`` row extracted by
    ``load_context`` for ``identify_case``'s routing decision (conversation-
    model.md's ambiguity rule needs to know how many open cases exist and
    which is most recently active) and for the future ``classify_intent``/
    ``classify_severity`` prompts (so the model can see what's already
    open before deciding whether a message continues one of them).

    "Open" here means any ``cases.status`` conversation-model.md treats as
    still active work: ``open``, ``awaiting_approval``, ``awaiting_tenant``,
    ``reopened`` — it deliberately EXCLUDES ``resolved`` (resolved cases are
    matched, if at all, via the separate reopen-window logic in
    ``app/agent/case_lifecycle.py``, not via this list).

    ``last_activity_at`` is an ISO-8601 UTC string, same convention as
    ``ChannelMessage.timestamp``.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: UUID
    status: str
    severity: str | None = None
    intent: str | None = None
    title: str | None = None
    last_activity_at: str
