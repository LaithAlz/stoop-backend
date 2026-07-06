"""Bridges the doc's simplified scenario ``context`` block onto the real
agent types (``WeatherSnapshot``, ``VulnerableOccupant``, the
``heating_season`` jsonb shape, and the ``RefusalFlag`` enum).

Every function here is a pure, deterministic translation -- no I/O, no
Anthropic calls. Kept separate from ``evals/runner.py`` so the "doc format
-> production type" bridging decisions are easy to find and audit in one
place; each one is documented because it is an interpretation this task's
instructions asked to be flagged, not an invented behavior change to
product code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.agent.schemas import RefusalFlag, VulnerableOccupant, WeatherSnapshot
from evals.scenario import Scenario

# ---------------------------------------------------------------------------
# Refusal-flag naming drift: doc <-> schema
# ---------------------------------------------------------------------------

REFUSAL_FLAG_DOC_MAP: dict[str, RefusalFlag] = {
    # eval-scenarios-v1.md's name -> app.agent.schemas.RefusalFlag
    "legal_rent_ltb": RefusalFlag.legal_rent_ltb,
    "access_codes": RefusalFlag.access_codes,
}
"""**Now an IDENTITY map** (2026-07-05 correction): ``eval-scenarios-v1.md``
(F1/F2) previously used illustrative flag names (``legal_rent_topic``/
``access_control``) that had drifted from ``app.agent.schemas.
RefusalFlag``'s real enum vocabulary (``legal_rent_ltb``/``access_codes``).
The doc has been corrected (see its 2026-07-05 changelog note) to use the
real enum names directly, so every key here now equals its own value.
This map is KEPT ANYWAY, deliberately, as a tolerant bridge rather than
being deleted and replaced with direct ``RefusalFlag(name)`` construction:
a future scenario that reintroduces an illustrative/non-enum name (or a
typo) fails loudly here (``KeyError``) exactly like ``evals/scenario.py``'s
``extra="forbid"`` drift protection, rather than silently raising a
less-informative ``ValueError`` from the enum constructor two call frames
away."""


def map_refusal_flag(doc_name: str) -> RefusalFlag:
    """Translate one doc-style refusal-flag name to the real enum member.

    Raises ``KeyError`` (loudly, not silently) if a future scenario
    introduces a refusal-flag name this map doesn't know about -- exactly
    the "drift protection" the scenario schema itself enforces for other
    fields.
    """
    return REFUSAL_FLAG_DOC_MAP[doc_name]


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def weather_snapshot_for(scenario: Scenario) -> WeatherSnapshot | None:
    """Build the ``WeatherSnapshot`` ``classify_severity`` expects from the
    scenario's single ``outdoor_temp_c`` field.

    Bridging note: the rubric's no-heat EMERGENCY rule keys off "the
    outdoor temperature (current or forecast overnight low)" -- two
    distinct inputs -- but ``eval-scenarios-v1.md``'s YAML format only ever
    supplies one number, ``outdoor_temp_c``. This function applies that one
    value to BOTH ``current_temp_c`` and ``overnight_low_c`` since the doc
    does not model day/night separately; this is sufficient to exercise the
    -10 C threshold line the E3/U1 pair is deliberately designed around; it
    is not a claim that the two are otherwise interchangeable in
    production.

    Returns ``None`` only when the scenario supplies neither
    ``outdoor_temp_c`` nor ``heat_warning`` -- mirrors
    ``classify_severity``'s own "weather data is unavailable" branch
    (``state.get("weather")`` is legitimately ``None`` before
    ``load_context`` runs).
    """
    ctx = scenario.context
    if ctx.outdoor_temp_c is None and ctx.heat_warning is None:
        return None
    return WeatherSnapshot(
        current_temp_c=ctx.outdoor_temp_c,
        overnight_low_c=ctx.outdoor_temp_c,
        heat_warning=bool(ctx.heat_warning),
    )


# ---------------------------------------------------------------------------
# Vulnerable occupant
# ---------------------------------------------------------------------------


def vulnerable_occupant_for(scenario: Scenario) -> VulnerableOccupant | None:
    """The doc's ``vulnerable_occupant`` value (E3: ``infant``) is already
    spelled identically to the ``VulnerableOccupant`` enum member name -- a
    direct construction, not a translation table (unlike the refusal-flag
    case above, where the names genuinely differ)."""
    raw = scenario.context.tenant.vulnerable_occupant
    return VulnerableOccupant(raw) if raw else None


# ---------------------------------------------------------------------------
# Heating season
# ---------------------------------------------------------------------------

HEATING_SEASON_DEFAULT: dict[str, str] = {"start": "Sep 15", "end": "Jun 1"}
"""``properties.heating_season`` (schema-v1.md) is a jsonb ``{start, end}``
pair; ``severity-rubric-v1.md``'s implementation notes give the concrete
Toronto Property Standards default (Sept 15 - Jun 1). The eval-scenarios
-v1.md YAML format only ever expresses this as a bare boolean
(``heating_season: true``) -- this constant is what that boolean expands
to when true, so ``classify_severity``'s real ``_build_user_content``
helper (which expects the dict shape) receives a well-formed value instead
of a bool it cannot call ``.get()`` on."""


def heating_season_dict_for(scenario: Scenario) -> dict[str, Any] | None:
    return dict(HEATING_SEASON_DEFAULT) if scenario.context.heating_season else None


# ---------------------------------------------------------------------------
# "now"
# ---------------------------------------------------------------------------

_REFERENCE_DATE = (2026, 1, 15)
"""Arbitrary reference date -- the doc's scenarios only ever specify a
local time-of-day (``time_local: "HH:MM"``), never a calendar date, and
nothing in the rubric or either node depends on the DATE (only the
displayed "current date/time" line and, separately, the scenario's own
explicit ``heating_season``/``outdoor_temp_c`` fields, which are supplied
independently of this value)."""


def now_for(scenario: Scenario) -> datetime:
    hour_str, minute_str = scenario.context.time_local.split(":")
    return datetime(*_REFERENCE_DATE, int(hour_str), int(minute_str), tzinfo=UTC)


__all__: list[str] = [
    "HEATING_SEASON_DEFAULT",
    "REFUSAL_FLAG_DOC_MAP",
    "heating_season_dict_for",
    "map_refusal_flag",
    "now_for",
    "vulnerable_occupant_for",
    "weather_snapshot_for",
]
