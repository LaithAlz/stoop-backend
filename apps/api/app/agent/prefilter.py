"""Tier-0 deterministic emergency pre-filter.

Pure functions only — no I/O, no network, no DB, no Anthropic calls.
Sub-millisecond per call.  Called synchronously in the Twilio webhook
handler BEFORE the agent graph is invoked.

Version coupling
----------------
``PREFILTER_VERSION`` is pinned to the rubric version it was generated
from.  When the rubric is bumped to v1.1 a new prefilter module is
created (prefilter_v2.py) — never edit the patterns in place.

``prefilter v1.0  ⟺  rubric v1.0``

HARD trigger list source
------------------------
Patterns are derived from the EMERGENCY section of
``docs/02-product/severity-rubric-v1.md`` v1.0 (frozen 2026-06-11) and
the canonical category list in ``docs/02-product/emergency-prefilter.md``.

Guard semantics
---------------
A guard suppresses *only* the specific trigger matches whose span falls
inside (or overlaps with) the guard condition's own match span.  The
algorithm:

1. Normalize the text (lowercase, punctuation → space, collapse whitespace).
2. For each HARD trigger pattern, find ALL match spans in the normalized
   string.  Each (trigger_index, span) pair is a "hit slot".
3. For each guard, find all match spans of the guard's condition pattern.
   If any guard match span exists, record the guard name (it has activated).
4. For each active hit slot, check whether the trigger match span is
   fully covered by ANY guard match span in the same protected category.
   If so, remove that hit slot.
5. After all guards: if ANY hit slot remains → ``hard_hit = True``.

Key invariants:
- A guard only suppresses trigger matches that fall WITHIN its own matched
  span.  This prevents the guard trigger_sub from matching an independent
  later occurrence of the same keyword.
- Guards are always recorded in ``guards`` when their pattern matches,
  whether or not they successfully suppressed anything (so the review log
  is complete in both the suppressed and the overridden case).
- A fire-drill guard matching "fire drill" only neutralises the "fire"
  token inside "fire drill", not a subsequent independent "fire" token.

Public API
----------
``check(text: str) -> PrefilterResult``
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agent.schemas import PrefilterResult

# ---------------------------------------------------------------------------
# Version pin — must move with the rubric
# ---------------------------------------------------------------------------

PREFILTER_VERSION: str = "1.0"

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Replace any non-alphanumeric character (except spaces) with a space so that
# "Burst-Pipe!!!" normalizes to "burst pipe".
_PUNCT_RE: re.Pattern[str] = re.compile(r"[^a-z0-9\s]")
_WS_RE: re.Pattern[str] = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    lowered = text.lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", no_punct).strip()


# ---------------------------------------------------------------------------
# Internal pattern helpers
# ---------------------------------------------------------------------------


def _re(pattern: str) -> re.Pattern[str]:
    """Compile a regex with IGNORECASE (text is already lowercased but
    this is a safety net) and no multiline/dotall — single-message text."""
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# HARD trigger definitions
# ---------------------------------------------------------------------------
# Each trigger is a (category, compiled_pattern) pair.  One message can
# fire multiple categories; all that fire are recorded.
#
# Word-boundary note: \\b is used broadly but the bias rule applies — when
# a word boundary would cause a miss on a realistic tenant phrasing, we
# drop it.  "fire" uses \\b on both sides so "campfire" doesn't match, but
# the bias rule means we also check "fire" in compound phrases like
# "there's a fire in the hallway" which the bare \\bfire\\b handles fine.


@dataclass(frozen=True)
class _Trigger:
    category: str
    pattern: re.Pattern[str]


_HARD_TRIGGERS: list[_Trigger] = [
    # ------------------------------------------------------------------ fire
    # "fire" as a standalone word.
    # Word boundaries prevent matching "campfire", "fireplace", "fired" etc.
    # Guards handle "fire drill" and "fire alarm test" as false-positive cases.
    _Trigger("fire", _re(r"\bfire\b")),
    # "smoke" near smell/filling/everywhere (within 40 chars either direction)
    _Trigger("fire", _re(r"\bsmoke\b.{0,40}\b(smell|filling|everywhere)\b")),
    _Trigger("fire", _re(r"\b(smell|filling|everywhere)\b.{0,40}\bsmoke\b")),
    # "burning smell" / "smell of burning"
    _Trigger("fire", _re(r"\bburning\s+smell\b")),
    _Trigger("fire", _re(r"\bsmell\s+of\s+burning\b")),
    # ---------------------------------------------------------------- gas_co
    # "gas" near smell/leak/smells (within 40 chars either direction)
    _Trigger("gas_co", _re(r"\bgas\b.{0,40}\b(smell|leak|smells)\b")),
    _Trigger("gas_co", _re(r"\b(smell|leak|smells)\b.{0,40}\bgas\b")),
    # carbon monoxide (spelled out)
    _Trigger("gas_co", _re(r"\bcarbon\s+monoxide\b")),
    # CO alarm / CO detector
    _Trigger("gas_co", _re(r"\bco\s+(alarm|detector)\b")),
    # "alarm/detector going off" or "sounding" — covers CO alarm sounding
    _Trigger("gas_co", _re(r"\b(alarm|detector)\s+going\s+off\b")),
    _Trigger("gas_co", _re(r"\b(alarm|detector)\s+sounding\b")),
    # --------------------------------------------------------------- water
    # flood / flooding
    _Trigger("water", _re(r"\bflood(ing)?\b")),
    # burst pipe
    _Trigger("water", _re(r"\bburst\s+pipe\b")),
    # "water" near active-flow words (within 60 chars either direction)
    _Trigger(
        "water",
        _re(r"\bwater\b.{0,60}\b(pouring|gushing|coming\s+through|through\s+the\s+ceiling)\b"),
    ),
    _Trigger(
        "water",
        _re(r"\b(pouring|gushing|coming\s+through|through\s+the\s+ceiling)\b.{0,60}\bwater\b"),
    ),
    # sewage (backup is always an emergency)
    _Trigger("water", _re(r"\bsewage\b")),
    # ------------------------------------------------------------- security
    _Trigger("security", _re(r"\bbreaking\s+in\b")),
    _Trigger("security", _re(r"\bbroke\s+in\b")),
    _Trigger("security", _re(r"\bbreak\s+in\b")),
    _Trigger("security", _re(r"\bintruder\b")),
    _Trigger("security", _re(r"\bsomeone\s+is\s+trying\s+to\s+get\s+in\b")),
    # -------------------------------------------------------------- person
    _Trigger("person", _re(r"\b911\b")),
    _Trigger("person", _re(r"\bambulance\b")),
    # "can't breathe" / "cant breathe" (apostrophe stripped by normalization)
    _Trigger("person", _re(r"\bcan\s*t\s+breathe\b")),
    _Trigger("person", _re(r"\bunconsci(ous)?\b")),
    # Additional patterns clearly implied by rubric EMERGENCY "medical emergency"
    # Bias rule: include rather than omit.
    _Trigger("person", _re(r"\bheart\s+attack\b")),
    _Trigger("person", _re(r"\bseizure\b")),
    _Trigger("person", _re(r"\bnot\s+breathing\b")),
    # Elevator entrapment is listed explicitly in the rubric EMERGENCY section.
    _Trigger("person", _re(r"\belevator\s+(entrapment|stuck|trapped)\b")),
    _Trigger("person", _re(r"\btrapped\s+in\s+(the\s+)?elevator\b")),
]

# ---------------------------------------------------------------------------
# Guard definitions
# ---------------------------------------------------------------------------
# Each guard defines:
#   name      — recorded in PrefilterResult.guards when this guard activates.
#   pattern   — the guard condition; MUST match for the guard to activate.
#   protects  — which trigger category this guard can suppress.
#
# Suppression logic (see module docstring):
#   A trigger hit slot (trigger_index, span) is suppressed when:
#     1. The trigger is in guard.protects.
#     2. The trigger match span falls WITHIN (overlaps) ANY guard pattern
#        match span in the normalized string.
#
# By anchoring suppression to the guard's OWN match span (not a secondary
# sub-pattern), we prevent a guard from silencing an independent later
# occurrence of the same keyword that is NOT part of the guarded phrase.
#
# Example: "fire drill earlier but real fire in the stairwell"
#   - guard "fire_drill" matches span [0,9] ("fire drill")
#   - trigger \bfire\b matches at span [0,4] and also at span [??] for "real fire"
#   - only the first "fire" is within [0,9] → suppressed
#   - second "fire" is OUTSIDE [0,9] → NOT suppressed → hard_hit=True


@dataclass(frozen=True)
class _Guard:
    name: str
    pattern: re.Pattern[str]
    protects: str


_GUARDS: list[_Guard] = [
    # smoke detector/alarm + battery/chirp/beep → routine battery-chirp case.
    # Rubric ROUTINE: "smoke-detector battery chirp (single intermittent chirp —
    # a CONTINUOUS alarm is EMERGENCY)".
    # Suppresses fire-category trigger matches whose span falls within the
    # guard match (i.e. the "smoke" in "smoke detector battery chirping").
    _Guard(
        name="smoke_detector_battery",
        pattern=_re(
            r"\bsmoke\s+(detector|alarm)\b.{0,80}\b(battery|chirp|chirping|beep|beeping|low\s+battery)\b"
        ),
        protects="fire",
    ),
    _Guard(
        name="smoke_detector_battery",
        pattern=_re(
            r"\b(battery|chirp|chirping|beep|beeping|low\s+battery)\b.{0,80}\bsmoke\s+(detector|alarm)\b"
        ),
        protects="fire",
    ),
    # "fire drill" — the word "fire" appears inside the phrase "fire drill"
    # and must not trigger the EMERGENCY path.
    _Guard(
        name="fire_drill",
        pattern=_re(r"\bfire\s+drill\b"),
        protects="fire",
    ),
    # "fire alarm test" / "fire alarm testing" — scheduled test, not real fire.
    _Guard(
        name="fire_alarm_test",
        pattern=_re(r"\bfire\s+alarm\s+test(ing)?\b"),
        protects="fire",
    ),
]

# ---------------------------------------------------------------------------
# SOFT annotation definitions
# ---------------------------------------------------------------------------
# SOFT patterns never fire the emergency protocol alone.  They are collected
# and surfaced to the classifier and degraded-mode logic.


@dataclass(frozen=True)
class _Soft:
    name: str
    pattern: re.Pattern[str]


_SOFT_PATTERNS: list[_Soft] = [
    _Soft("no_heat", _re(r"\bno\s+heat\b")),
    _Soft("no_heat", _re(r"\bheat\s+(is\s+)?(out|off|not\s+working|broken)\b")),
    _Soft("freezing", _re(r"\bfreezing\b")),
    _Soft("sparks", _re(r"\bspark(s|ing)?\b")),
    # "leak" — a contained drip/leak; burst pipe and flood are HARD water.
    _Soft("leak", _re(r"\bleak(s|ing|ed)?\b")),
    _Soft("locked_out", _re(r"\blocked\s+out\b")),
]


# ---------------------------------------------------------------------------
# Internal data type for hit tracking
# ---------------------------------------------------------------------------


@dataclass
class _HitSlot:
    trigger_idx: int
    start: int
    end: int


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def check(text: str) -> PrefilterResult:
    """Classify *text* with the Tier-0 deterministic pre-filter.

    Returns a :class:`~app.agent.schemas.PrefilterResult` with:

    - ``hard_hit`` — True iff at least one HARD trigger remained un-guarded.
    - ``categories`` — sorted list of HARD categories that fired.
    - ``soft_annotations`` — sorted list of SOFT matches.
    - ``guards`` — sorted list of guard names that activated (recorded
      whenever a guard condition matches, even when hard_hit is True).

    Pure function: no I/O, no side effects, no global state mutation.
    """
    norm = _normalize(text)

    # ------------------------------------------------------------------
    # Step 1: collect all trigger hit slots
    # A slot = one regex match of one trigger pattern in the normalized text.
    # Multiple slots for the same trigger index are possible (multiple matches).
    # ------------------------------------------------------------------
    hit_slots: list[_HitSlot] = []
    for i, trigger in enumerate(_HARD_TRIGGERS):
        for m in trigger.pattern.finditer(norm):
            hit_slots.append(_HitSlot(trigger_idx=i, start=m.start(), end=m.end()))

    # ------------------------------------------------------------------
    # Step 2: apply guards
    #
    # For each guard, find all match spans of the guard's condition pattern.
    # If any match exists: record the guard name (it activated).
    # Then suppress every hit slot in guard.protects whose span falls
    # WITHIN (overlaps) any guard match span.
    #
    # "Overlap" means: not (slot.end <= guard_start or slot.start >= guard_end)
    #
    # This confines suppression to the literal text region matched by the guard
    # phrase, so an independent later occurrence of the keyword is NOT silenced.
    # ------------------------------------------------------------------
    fired_guards: set[str] = set()
    suppressed: set[int] = set()  # indices into hit_slots

    for guard in _GUARDS:
        guard_matches = list(guard.pattern.finditer(norm))
        if not guard_matches:
            continue

        # The guard condition fired — record it unconditionally.
        fired_guards.add(guard.name)

        guard_spans: list[tuple[int, int]] = [(gm.start(), gm.end()) for gm in guard_matches]

        for slot_idx, slot in enumerate(hit_slots):
            if slot_idx in suppressed:
                continue
            trigger = _HARD_TRIGGERS[slot.trigger_idx]
            if trigger.category != guard.protects:
                continue
            # Suppress this slot if it overlaps any guard match span.
            for gs, ge in guard_spans:
                if not (slot.end <= gs or slot.start >= ge):
                    suppressed.add(slot_idx)
                    break

    # ------------------------------------------------------------------
    # Step 3: collect fired categories from un-suppressed slots
    # ------------------------------------------------------------------
    fired_categories: set[str] = set()
    for slot_idx, slot in enumerate(hit_slots):
        if slot_idx not in suppressed:
            fired_categories.add(_HARD_TRIGGERS[slot.trigger_idx].category)

    hard_hit = bool(fired_categories)

    # ------------------------------------------------------------------
    # Step 4: SOFT annotations
    # ------------------------------------------------------------------
    soft_found: set[str] = set()
    for soft in _SOFT_PATTERNS:
        if soft.pattern.search(norm):
            soft_found.add(soft.name)

    return PrefilterResult(
        hard_hit=hard_hit,
        categories=sorted(fired_categories),
        soft_annotations=sorted(soft_found),
        guards=sorted(fired_guards),
    )
