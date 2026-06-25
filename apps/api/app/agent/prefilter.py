r"""Tier-0 deterministic emergency pre-filter.

Pure functions only -- no I/O, no network, no DB, no Anthropic calls.
Sub-millisecond per call.  Called synchronously in the Twilio webhook
handler BEFORE the agent graph is invoked.

Version coupling
----------------
``PREFILTER_VERSION`` is pinned to the rubric version it was generated
from.  When the rubric is bumped to v1.1 a new prefilter module is
created (prefilter_v2.py) -- never edit the patterns in place.

``prefilter v1.0  <->  rubric v1.0``

HARD trigger list source
------------------------
Patterns are derived from the EMERGENCY section of
``docs/02-product/severity-rubric-v1.md`` v1.0 (frozen 2026-06-11) and
the canonical category list in ``docs/02-product/emergency-prefilter.md``.

Guard semantics (re-architected from d8c83f1)
---------------------------------------------
The old implementation recorded the full match span of a proximity trigger
and the full span of a guard's ``.{0,80}`` window.  This caused two failure
modes:

  1. Guard over-suppression: a proximity trigger anchored on the FIRST
     keyword occurrence (inside the guard phrase) had its full span overlap
     the guard span, so it was suppressed even though the proximity
     condition was actually satisfied by a LATER, independent keyword
     occurrence.

  2. Wide-window suppression: the guard's ``.{0,80}`` tail stretched its
     suppression span over unrelated downstream tokens (e.g. "fire" inside
     "fire alarm" when the guard was triggered by "smoke detector chirping").

The fixed algorithm uses ANCHOR-TOKEN suppression:

1. Normalize the text (lowercase, punctuation -> space, collapse whitespace).
2. For each trigger, find all match spans.  For SIMPLE triggers (no
   proximity anchor needed) the full match span IS the anchor span.
   For PROXIMITY triggers: enumerate every anchor keyword token; for each
   occurrence check whether a proximity word appears within window_chars;
   record a hit slot keyed on the ANCHOR token span (not the full window).
3. For each guard, find all match spans of the guard's CORE pattern
   (the literal phrase span only -- e.g. ``\bsmoke\s+(detector|alarm)\b``
   -- NOT the ``{0,80}`` window).  If the full guard pattern matches,
   record the guard name.  Suppression uses only the core-pattern spans.
4. For each active hit slot, check whether the hit's ANCHOR token span
   falls inside ANY guard suppression span in the same protected category.
   If so, remove that hit slot.
5. After all guards: if ANY hit slot remains -> ``hard_hit = True``.

Key invariants:
- A proximity trigger hit is keyed on each qualifying ANCHOR keyword token,
  not the full match.  Multiple anchor occurrences in one message each
  produce independent hit slots.
- A guard suppresses only anchor tokens that fall WITHIN its CORE phrase
  span (e.g. the ``smoke detector`` tokens), never tokens downstream of
  the ``.{0,80}`` window.
- Guards are always recorded in ``guards`` when their full pattern matches,
  whether or not they successfully suppressed anything.
- INVARIANT: if ANY anchor keyword occurrence satisfies a trigger condition
  and that anchor token does NOT fall inside ANY guard core span,
  ``hard_hit = True``.

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
# Each trigger is either:
#   - A SIMPLE trigger: one compiled pattern whose full match span is the
#     anchor span recorded in the hit slot.
#   - A PROXIMITY trigger: an anchor_pattern (finds the keyword token) plus
#     a proximity_pattern applied to a window around each anchor occurrence.
#     Hit slots are keyed on the ANCHOR token span, not the full window.
#
# This architecture prevents the guard-over-suppression bug: even when an
# earlier keyword occurrence falls inside a guard phrase, a later independent
# occurrence of the same keyword that satisfies the proximity condition
# produces its OWN hit slot at the later token's position, which is outside
# the guard core span and therefore NOT suppressed.
#
# Word-boundary note: \\b is used broadly but the bias rule applies — when
# a word boundary would cause a miss on a realistic tenant phrasing, we
# drop it.  "fire" uses \\b on both sides so "campfire" doesn't match, but
# the bias rule means we also check "fire" in compound phrases like
# "there's a fire in the hallway" which the bare \\bfire\\b handles fine.


@dataclass(frozen=True)
class _SimpleTrigger:
    """A trigger whose match span is directly the anchor span.

    ``suppressible=False`` exempts the trigger from ALL guard suppression —
    used for triggers that describe a condition a guard must never silence
    (e.g. a CONTINUOUS smoke alarm: the rubric says it is EMERGENCY even
    though the message may also mention a battery, which would otherwise
    activate the battery-chirp guard).
    """

    category: str
    pattern: re.Pattern[str]
    suppressible: bool = True


@dataclass(frozen=True)
class _ProximityTrigger:
    """A trigger that finds an anchor keyword and checks proximity condition.

    ``anchor_pattern`` finds every occurrence of the keyword token.
    ``proximity_fwd`` is checked against text from anchor_start to
    anchor_end + window_chars.
    ``proximity_bwd`` is checked against text from anchor_start - window_chars
    to anchor_end.
    Either fwd OR bwd hit counts.
    """

    category: str
    anchor_pattern: re.Pattern[str]
    proximity_fwd: re.Pattern[str] | None
    proximity_bwd: re.Pattern[str] | None
    window_chars: int = 60
    suppressible: bool = True


_Trigger = _SimpleTrigger | _ProximityTrigger


def _make_proximity(
    category: str,
    anchor: str,
    proximity_words: str,
    window: int = 40,
    allow_bwd: bool = True,
) -> list[_ProximityTrigger]:
    """Return a list of forward and optionally backward proximity triggers.

    ``anchor`` is the regex for the anchor keyword (e.g. ``r"\\bsmoke\\b"``).
    ``proximity_words`` is the alternation group for the proximity condition
    (e.g. ``r"(smell|filling|everywhere)"``).
    """
    anchor_pat = _re(anchor)
    prox_pat = _re(r"\b" + proximity_words + r"\b")
    result: list[_ProximityTrigger] = [
        _ProximityTrigger(
            category=category,
            anchor_pattern=anchor_pat,
            proximity_fwd=prox_pat,
            proximity_bwd=prox_pat if allow_bwd else None,
            window_chars=window,
        )
    ]
    return result


_HARD_TRIGGERS: list[_Trigger] = [
    # ------------------------------------------------------------------ fire
    # "fire" as a standalone word.
    # Word boundaries prevent matching "campfire", "fireplace", "fired" etc.
    # Guards handle "fire drill" and "fire alarm test" as false-positive cases.
    _SimpleTrigger("fire", _re(r"\bfire\b")),
    # "smoke" near smell/filling/everywhere — PROXIMITY trigger anchored on
    # each "smoke" token independently.  This prevents the guard from
    # suppressing a later independent "smoke" that satisfies the condition.
    *_make_proximity("fire", r"\bsmoke\b", r"(smell|filling|everywhere)"),
    # "burning smell" / "smell of burning"
    _SimpleTrigger("fire", _re(r"\bburning\s+smell\b")),
    _SimpleTrigger("fire", _re(r"\bsmell\s+of\s+burning\b")),
    # Continuous smoke alarm (rubric: a CONTINUOUS alarm is EMERGENCY).
    # Two patterns: alarm first, then modifier; and modifier first, then alarm.
    # suppressible=False: these MUST fire even when a "battery"/"low battery"
    # word co-occurs and activates the battery-chirp guard (whose core span
    # "smoke (detector|alarm)" overlaps the start of these matches). A blaring
    # / nonstop alarm is an emergency regardless of whether the tenant also
    # wonders aloud about the battery. (Safety review: this exemption closes a
    # reintroduced miss.)
    _SimpleTrigger(
        "fire",
        _re(
            r"\bsmoke\s+(alarm|detector)\b.{0,30}"
            r"\b(blaring|wont\s+stop|won\s*t\s+stop|continuous|nonstop|going\s+off)\b"
        ),
        suppressible=False,
    ),
    _SimpleTrigger(
        "fire",
        _re(
            r"\b(blaring|wont\s+stop|won\s*t\s+stop|continuous|nonstop)\b.{0,30}"
            r"\bsmoke\s+(alarm|detector)\b"
        ),
        suppressible=False,
    ),
    # ---------------------------------------------------------------- gas_co
    # "gas" near smell/leak/smells (within 40 chars either direction)
    *_make_proximity("gas_co", r"\bgas\b", r"(smell|leak|smells|leaking)"),
    # "leak" near "gas" (reverse order proximity)
    *_make_proximity("gas_co", r"\bleak(s|ing)?\b", r"gas"),
    # carbon monoxide (spelled out)
    _SimpleTrigger("gas_co", _re(r"\bcarbon\s+monoxide\b")),
    # CO alarm / CO detector
    _SimpleTrigger("gas_co", _re(r"\bco\s+(alarm|detector)\b")),
    # "alarm/detector going off" or "sounding" — covers CO alarm sounding
    _SimpleTrigger("gas_co", _re(r"\b(alarm|detector)\s+going\s+off\b")),
    _SimpleTrigger("gas_co", _re(r"\b(alarm|detector)\s+sounding\b")),
    # --------------------------------------------------------------- water
    # flood / flooding
    _SimpleTrigger("water", _re(r"\bflood(ing)?\b")),
    # burst pipe (both word orders)
    _SimpleTrigger("water", _re(r"\bburst\s+pipe\b")),
    _SimpleTrigger("water", _re(r"\bpipe\b.{0,20}\bburst\b")),
    # "water" near active-flow words (within 60 chars either direction)
    *_make_proximity(
        "water",
        r"\bwater\b",
        r"(pouring|gushing|coming\s+through|through\s+the\s+ceiling)",
        window=60,
    ),
    # water + electrical contact (rubric: water on electrical = EMERGENCY)
    *_make_proximity(
        "water",
        r"\bwater\b",
        r"(outlet|electrical|breaker|panel|wiring|socket|light\s+fixture)",
        window=40,
    ),
    # sewage (backup is always an emergency)
    _SimpleTrigger("water", _re(r"\bsewage\b")),
    # ------------------------------------------------------------- security
    _SimpleTrigger("security", _re(r"\bbreaking\s+in(to)?\b")),
    _SimpleTrigger("security", _re(r"\bbroke\s+in(to)?\b")),
    _SimpleTrigger("security", _re(r"\bbreak\s+in(to)?\b")),
    _SimpleTrigger("security", _re(r"\bintruder\b")),
    _SimpleTrigger("security", _re(r"\bsomeone\s+is\s+trying\s+to\s+get\s+in\b")),
    # -------------------------------------------------------------- person
    _SimpleTrigger("person", _re(r"\b911\b")),
    _SimpleTrigger("person", _re(r"\bambulance\b")),
    # breathing distress — "can't breathe", "cannot breathe", "can not breathe",
    # "cant breath" (apostrophe stripped by normalization, trailing 'e' optional)
    _SimpleTrigger("person", _re(r"\b(can\s*t|cannot|can\s+not)\s+breath(e)?\b")),
    # proximity: trouble/struggling/hard near breathing
    _SimpleTrigger(
        "person",
        _re(r"\b(trouble|struggling|hard|cant|cannot)\b.{0,20}\bbreath(e|ing)?\b"),
    ),
    _SimpleTrigger("person", _re(r"\bunconsci(ous)?\b")),
    # Additional patterns clearly implied by rubric EMERGENCY "medical emergency"
    # Bias rule: include rather than omit.
    _SimpleTrigger("person", _re(r"\bheart\s+attack\b")),
    _SimpleTrigger("person", _re(r"\bseizure\b")),
    _SimpleTrigger("person", _re(r"\bnot\s+breathing\b")),
    # overdose(d) / collapsed — strong medical emergency signals
    _SimpleTrigger("person", _re(r"\boverdose(d)?\b")),
    _SimpleTrigger("person", _re(r"\bcollapsed\b")),
    # Elevator entrapment — both word orders + gap form
    _SimpleTrigger("person", _re(r"\belevator\s+(entrapment|stuck|trapped)\b")),
    _SimpleTrigger("person", _re(r"\btrapped\s+in\s+(the\s+|an\s+)?elevator\b")),
    _SimpleTrigger("person", _re(r"\bstuck\s+in\s+(the\s+|an\s+)?elevator\b")),
    # "elevator" near stuck/trapped/entrapment with a gap allowed
    _SimpleTrigger(
        "person",
        _re(r"\belevator\b.{0,15}\b(stuck|trapped|entrapment|not\s+moving)\b"),
    ),
]

# ---------------------------------------------------------------------------
# Guard definitions
# ---------------------------------------------------------------------------
# Each guard defines:
#   name        — recorded in PrefilterResult.guards when this guard activates.
#   pattern     — the FULL guard condition (must match for guard to activate).
#   core_pattern — the CORE phrase span used for suppression.  Anchor tokens
#                 whose span falls WITHIN a core_pattern match are suppressed.
#                 This is tighter than pattern and does NOT include the
#                 ``.{0,80}`` proximity tail.
#   protects    — which trigger category this guard can suppress.
#
# Suppression logic:
#   A trigger hit slot (anchor_start, anchor_end) is suppressed when:
#     1. The trigger is in guard.protects.
#     2. The trigger anchor token span falls WITHIN (overlaps) ANY core_pattern
#        match span in the normalized string.
#
# By anchoring suppression to the CORE phrase span (the literal guarded keyword
# phrase, e.g. "smoke detector") rather than the full window match, we prevent
# a guard from silencing downstream independent occurrences of the same keyword.
#
# Example: "smoke detector and the fire alarm keep chirping"
#   - guard core_pattern "smoke (detector|alarm)" matches span [4,18]
#   - trigger \bfire\b matches at span [27,31]
#   - [27,31] does NOT overlap [4,18] → NOT suppressed → hard_hit=True
#
# Example: "smoke detector battery chirping"
#   - guard core_pattern "smoke (detector|alarm)" matches span [0,14]
#   - proximity trigger anchor "smoke" at span [0,5] falls inside [0,14]
#   - suppressed → hard_hit=False
#
# Example: "smoke detector chirping but smoke is filling the kitchen"
#   - guard core_pattern "smoke (detector|alarm)" matches span [0,14]
#   - anchor "smoke" at [0,5] → inside [0,14] → SUPPRESSED
#   - anchor "smoke" at [28,33] → OUTSIDE [0,14] → NOT suppressed → hard_hit=True


@dataclass(frozen=True)
class _Guard:
    name: str
    pattern: re.Pattern[str]  # Full guard condition (determines activation)
    core_pattern: re.Pattern[str]  # Core phrase span (determines suppression range)
    protects: str


_GUARDS: list[_Guard] = [
    # smoke detector/alarm + battery/chirp/beep → routine battery-chirp case.
    # Rubric ROUTINE: "smoke-detector battery chirp (single intermittent chirp —
    # a CONTINUOUS alarm is EMERGENCY)".
    # Core pattern: "smoke (detector|alarm)" — suppresses only smoke tokens
    # that are part of the "smoke detector/alarm" phrase itself.
    # The .{0,80} tail is in the full pattern (guard activation check) only.
    _Guard(
        name="smoke_detector_battery",
        pattern=_re(
            r"\bsmoke\s+(detector|alarm)\b.{0,80}\b(battery|chirp|chirping|beep|beeping|low\s+battery)\b"
        ),
        core_pattern=_re(r"\bsmoke\s+(detector|alarm)\b"),
        protects="fire",
    ),
    _Guard(
        name="smoke_detector_battery",
        pattern=_re(
            r"\b(battery|chirp|chirping|beep|beeping|low\s+battery)\b.{0,80}\bsmoke\s+(detector|alarm)\b"
        ),
        core_pattern=_re(r"\bsmoke\s+(detector|alarm)\b"),
        protects="fire",
    ),
    # "fire drill" — the word "fire" appears inside the phrase "fire drill"
    # and must not trigger the EMERGENCY path.
    # Core = full pattern (both are the same literal phrase, no tail window).
    _Guard(
        name="fire_drill",
        pattern=_re(r"\bfire\s+drill\b"),
        core_pattern=_re(r"\bfire\s+drill\b"),
        protects="fire",
    ),
    # "fire alarm test" / "fire alarm testing" — scheduled test, not real fire.
    _Guard(
        name="fire_alarm_test",
        pattern=_re(r"\bfire\s+alarm\s+test(ing)?\b"),
        core_pattern=_re(r"\bfire\s+alarm\s+test(ing)?\b"),
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
    anchor_start: int  # start of ANCHOR TOKEN span (not full match)
    anchor_end: int  # end of ANCHOR TOKEN span


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def _collect_hit_slots(norm: str) -> list[_HitSlot]:
    """Collect all trigger hit slots, keying each on its anchor token span.

    For _SimpleTrigger: the full match span is the anchor span.
    For _ProximityTrigger: find every anchor keyword token; for each,
    check whether a proximity word appears within window_chars in the
    forward or backward direction; record a hit slot at the anchor token's
    span when the condition is met.
    """
    hit_slots: list[_HitSlot] = []
    for i, trigger in enumerate(_HARD_TRIGGERS):
        if isinstance(trigger, _SimpleTrigger):
            for m in trigger.pattern.finditer(norm):
                hit_slots.append(
                    _HitSlot(trigger_idx=i, anchor_start=m.start(), anchor_end=m.end())
                )
        else:
            # _ProximityTrigger — enumerate every anchor token
            for am in trigger.anchor_pattern.finditer(norm):
                a_start, a_end = am.start(), am.end()
                hit = False
                if trigger.proximity_fwd is not None:
                    window = norm[a_start : a_end + trigger.window_chars]
                    if trigger.proximity_fwd.search(window):
                        hit = True
                if not hit and trigger.proximity_bwd is not None:
                    window = norm[max(0, a_start - trigger.window_chars) : a_end]
                    if trigger.proximity_bwd.search(window):
                        hit = True
                if hit:
                    hit_slots.append(
                        _HitSlot(trigger_idx=i, anchor_start=a_start, anchor_end=a_end)
                    )
    return hit_slots


def check(text: str) -> PrefilterResult:
    """Classify *text* with the Tier-0 deterministic pre-filter.

    Returns a :class:`~app.agent.schemas.PrefilterResult` with:

    - ``hard_hit`` — True iff at least one HARD trigger remained un-guarded.
    - ``categories`` — sorted list of HARD categories that fired.
    - ``soft_annotations`` — sorted list of SOFT matches.
    - ``guards`` — sorted list of guard names that activated (recorded
      whenever a guard condition matches, even when hard_hit is True).

    Pure function: no I/O, no side effects, no global state mutation.

    Suppression invariant: a guard suppresses a hit slot ONLY when the
    slot ANCHOR TOKEN falls within the guard CORE PHRASE span.
    The .{0,80} tail of a guard pattern NEVER suppresses downstream
    independent keyword occurrences.
    """
    norm = _normalize(text)

    # ------------------------------------------------------------------
    # Step 1: collect all trigger hit slots (anchor-token based)
    # ------------------------------------------------------------------
    hit_slots = _collect_hit_slots(norm)

    # ------------------------------------------------------------------
    # Step 2: apply guards
    #
    # For each guard, find all match spans of the guard's FULL pattern
    # (to determine if the guard activates), then find all match spans of
    # the CORE pattern (to determine which anchor tokens are suppressed).
    # ------------------------------------------------------------------
    fired_guards: set[str] = set()
    suppressed: set[int] = set()  # indices into hit_slots

    for guard in _GUARDS:
        # Check full pattern for guard activation
        full_matches = list(guard.pattern.finditer(norm))
        if not full_matches:
            continue

        # Guard activated — record unconditionally
        fired_guards.add(guard.name)

        # Collect CORE phrase spans for suppression
        core_spans: list[tuple[int, int]] = [
            (cm.start(), cm.end()) for cm in guard.core_pattern.finditer(norm)
        ]

        for slot_idx, slot in enumerate(hit_slots):
            if slot_idx in suppressed:
                continue
            trigger = _HARD_TRIGGERS[slot.trigger_idx]
            if not trigger.suppressible:
                # Exempt from all guard suppression (e.g. continuous alarm).
                continue
            if trigger.category != guard.protects:
                continue
            # Suppress if the ANCHOR TOKEN overlaps any core phrase span.
            for cs, ce in core_spans:
                if not (slot.anchor_end <= cs or slot.anchor_start >= ce):
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
