"""Eval scenario schema + YAML loader (#35/#36).

Format source of truth: ``docs/02-product/eval-scenarios-v1.md`` (v1.0,
approved 2026-06-11). Every field on :class:`Scenario` below corresponds to
a field the doc's ```yaml blocks show for the 10 approved scenarios
(E1-E3, U1-U3, R1-R2, F1-F2) -- with two authorized, documented additions:

1. ``prefilter_must_fire`` -- NOT literally shown inside the doc's own
   ```yaml blocks, but ``docs/02-product/emergency-prefilter.md``
   explicitly instructs: "Eval scenarios E1 and E2 must trip Tier 0 (add
   ``prefilter_must_fire: true`` to both)" -- and issue #36's acceptance
   criteria repeats this verbatim ("E1, E2 carry prefilter_must_fire:
   true"). This module adds the field as a top-level scalar (sibling of
   ``expect``, since it is a Tier-0 assertion, not a classification/draft
   one). Beyond the two mandated ``true`` scenarios, every other canonical
   scenario in this harness also carries an explicit ``prefilter_must_fire:
   false`` -- a deliberate strengthening (not required by the doc, zero
   marginal API cost since Tier-0 is a pure function) that turns "Tier-0
   should not fire here" from an implicit assumption into an asserted
   regression net. See ``evals/runner.py``'s docstring for how the negative
   -prefilter suite (also ``prefilter_must_fire: false``, but additionally
   ``tier0_only: true``) differs.

2. ``tier0_only`` -- an eval-harness-only field (not from the doc at all),
   defaulting to ``False``. Set ``true`` on the R-class "detector chirping"
   negative-prefilter suite (``evals/scenarios/negative_prefilter/*.yaml``)
   so the runner skips the real classify_severity/draft_response Anthropic
   calls for those scenarios entirely -- they exist ONLY to exercise the
   deterministic Tier-0 filter (``app/agent/prefilter.py``), per
   ``emergency-prefilter.md``'s "a negative suite asserts the guards
   (R-class 'detector chirping' must NOT fire)". Running the real LLM on
   them would cost money to test something that is, by construction, a
   pure-function/no-I/O assertion already covered exhaustively by
   ``tests/test_prefilter.py`` -- this field keeps that saving explicit,
   not implicit.

Everything else below is a 1:1, verbatim-named projection of the doc's
YAML shape. ``model_config = ConfigDict(extra="forbid")`` at every level is
the drift protection the issue asks for: if a *future* revision of
eval-scenarios-v1.md adds a new field to a scenario block, loading that
scenario here raises a ``ValidationError`` immediately (loud failure) engineers
must consciously plumb through, rather than the new field being silently
dropped on the floor.
"""

from __future__ import annotations

import glob
import os
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SCENARIOS_DIR: str = os.path.join(os.path.dirname(__file__), "scenarios")
"""Root directory the loader globs recursively -- includes the top-level
10 canonical scenarios and the ``negative_prefilter/`` subdirectory."""


# ---------------------------------------------------------------------------
# context.tenant
# ---------------------------------------------------------------------------


class ScenarioTenant(BaseModel):
    """``context.tenant`` -- matches the doc's ``{ name, unit,
    vulnerable_occupant }`` inline mapping. ``vulnerable_occupant`` is only
    present on E3 in the doc (value ``infant``) -- the raw string is kept
    as-is here; ``evals/context.py`` maps it onto the real
    ``VulnerableOccupant`` enum (the doc's value already matches the enum
    member name, so this is a direct construction, not a translation
    table)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    unit: str
    vulnerable_occupant: str | None = None


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


class ScenarioContext(BaseModel):
    """``context`` block. Fields absent from a given scenario's YAML (e.g.
    R1/R2/F1/F2 never set ``outdoor_temp_c``) default to ``None`` -- see
    ``evals/context.py`` for how ``None`` is bridged into the real node's
    ``WeatherSnapshot``/``heating_season`` shapes."""

    model_config = ConfigDict(extra="forbid")

    property: str
    tenant: ScenarioTenant
    time_local: str
    outdoor_temp_c: float | None = None
    heat_warning: bool | None = None
    heating_season: bool | None = None


# ---------------------------------------------------------------------------
# expect
# ---------------------------------------------------------------------------


class ScenarioExpect(BaseModel):
    """``expect`` block -- the pass/fail criteria.

    ``rules_fired`` / ``modifier`` are only populated for E1-E3 in the doc
    (the rubric rule(s) that must fire). ``not_actions`` only appears on U1
    (``[call_landlord_now]`` -- the single most safety-critical negative
    assertion in the whole corpus). ``refusal_flags`` only appears on
    F1/F2, using ``app.agent.schemas.RefusalFlag``'s real enum values
    (``legal_rent_ltb``/``access_codes``) directly -- the doc previously
    used illustrative names (``legal_rent_topic``/``access_control``) that
    had drifted from the schema; corrected in the doc's 2026-07-05
    changelog note. ``evals/context.py``'s ``REFUSAL_FLAG_DOC_MAP`` still
    bridges this field (now an identity map, kept as a loud-failure
    tolerance layer rather than direct enum construction -- see that
    module's docstring).
    """

    model_config = ConfigDict(extra="forbid")

    severity: Literal["emergency", "urgent", "routine"]
    rules_fired: list[str] = Field(default_factory=list)
    modifier: str | None = None
    actions: list[str] = Field(default_factory=list)
    not_actions: list[str] = Field(default_factory=list)
    refusal_flags: list[str] = Field(default_factory=list)
    draft_must_include: list[str] = Field(default_factory=list)
    draft_must_not_include: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# top-level scenario
# ---------------------------------------------------------------------------


class Scenario(BaseModel):
    """One scenario -- one YAML file. ``category`` is the doc's own
    grouping (emergency/urgent/routine/refusal) and is NOT always equal to
    ``expect.severity`` -- F1/F2 are ``category: refusal`` but classify at
    ``expect.severity: routine`` (the rubric flags refusal topics
    independently of severity; see severity-rubric-v1.md's judgment call
    #6). ``evals/scoring.py``'s hard-fail/soft-fail gate keys off
    ``category`` (E-class/F-class = hard fail), never off ``expect.
    severity`` alone, for exactly this reason.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    category: Literal["emergency", "urgent", "routine", "refusal"]
    context: ScenarioContext
    message: str
    expect: ScenarioExpect
    rationale: str
    prefilter_must_fire: bool | None = None
    tier0_only: bool = False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_scenarios(
    *, scenarios_dir: str = SCENARIOS_DIR, include_tier0_only: bool = True
) -> list[Scenario]:
    """Load every ``*.yaml`` file under *scenarios_dir* (recursively, so the
    ``negative_prefilter/`` subdirectory is included) into validated
    :class:`Scenario` objects, sorted by file path for deterministic
    ordering.

    Raises ``pydantic.ValidationError`` on any unknown/missing/malformed
    field (drift protection) and ``ValueError`` on a duplicate ``id``
    across files.

    ``include_tier0_only=False`` excludes the negative-prefilter suite --
    used by callers that only want the 10 canonical (+ injection) LLM
    -graded scenarios.
    """
    paths = sorted(glob.glob(os.path.join(scenarios_dir, "**", "*.yaml"), recursive=True))
    scenarios: list[Scenario] = []
    seen_ids: dict[str, str] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        scenario = Scenario.model_validate(raw)
        if scenario.id in seen_ids:
            raise ValueError(
                f"duplicate scenario id {scenario.id!r}: {seen_ids[scenario.id]!r} and {path!r}"
            )
        seen_ids[scenario.id] = path
        if scenario.tier0_only and not include_tier0_only:
            continue
        scenarios.append(scenario)
    return scenarios


__all__: list[str] = [
    "SCENARIOS_DIR",
    "Scenario",
    "ScenarioContext",
    "ScenarioExpect",
    "ScenarioTenant",
    "load_scenarios",
]
