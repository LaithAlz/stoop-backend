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

1. Normalize the text: fold unicode (NFKD, strip combining marks) so
   accented letters collapse to their ASCII base (e.g. "éverywhere" ->
   "everywhere") BEFORE lowercasing/punctuation-stripping destroys them;
   collapse "9-1-1"-shaped digit runs to "911" BEFORE generic punctuation
   stripping turns each separator into its own space token; lowercase;
   strip punctuation -> space; collapse whitespace.
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

Tense/inflection-completeness sweep (post-#144, found building the #35/#36
eval harness)
--------------------------------------------------------------------------
The eval harness's E2 scenario ("the kitchen has smelled like gas since I
got home an hour ago" -- lifted VERBATIM from eval-scenarios-v1.md) did not
trip Tier-0, despite emergency-prefilter.md mandating that E1/E2 must.
Root cause: the ``gas_co`` proximity trigger's word list had "smell"/
"smells" (present tense) but never the PAST tense "smelled" at all -- a
hand-copied-alternation-drift defect, the exact class #144's commit
message already named. A full sweep of every OTHER trigger/soft-annotation
word list for the same defect class (a verb-based alternation missing one
or more of its base/3rd-person/past/progressive forms) found and fixed
several more instances (see each site's own inline comment for specifics):
``fire``'s smoke-proximity list (missing smelled/smelt/smelling/filled),
``gas_co``'s gas-proximity list AND its reverse leak-anchor (missing
smelled/smelt/smelling/leaked/leaks), ``water``'s ``flood`` trigger
(missing "flooded") and its active-flow proximity list (missing poured/
gushed/came through), ``person``'s ``overdose`` (missing "overdosing") and
``collapsed`` (missing base "collapse" and progressive "collapsing")
triggers, and the SOFT ``sparks`` annotation (missing "sparked" -- the
adjacent SOFT ``leak`` annotation already correctly had its "ed" form,
proving the two had drifted out of sync with each other). Deliberately
NOT touched in this pass: the continuous-alarm shared phrase list
(``_CONTINUOUS_ALARM_PHRASES``) and every GUARD word list (battery/chirp/
beep) -- both are suppression-side logic, already extensively hardened
across multiple prior safety-review rounds (see their own comments), and
out of scope for a TRIGGER-side tense-completion pass (a guard's word-list
gap has no ``hard_hit`` correctness impact -- see the issue report for the
proof). No new vocabulary was introduced anywhere in this pass: every
change only completes the inflection set of a verb ALREADY present in its
own alternation.

Public API
----------
``check(text: str) -> PrefilterResult``
"""

from __future__ import annotations

import re
import unicodedata
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

# "9-1-1" collapse — applied BEFORE generic punctuation stripping.
#
# Generic punctuation stripping turns every separator into its own space
# (`_PUNCT_RE` maps each non-alphanumeric char to a space independently), so
# "9-1-1" becomes "9 1 1" — three separate tokens — and `\b911\b` misses it.
#
# Fix chosen: a narrow, anchored pre-pass that collapses "9", <sep>, "1",
# <sep>, "1" into the literal "911" *before* the generic punctuation pass
# runs, where <sep> is one-or-more of space/dot/hyphen and is REQUIRED on
# BOTH sides (`+`, not `*`). This is preferred over loosening the trigger
# regex itself (e.g. matching `\b9\s*1\s*1\b` against the fully-normalized
# string) because a permissive *-quantified version also matches unrelated
# digit runs that happen to reduce to the same shape — e.g. requiring only
# an optional separator would collapse "$9.11" (a dollar amount: "9", ".",
# "11") into "911" too, a false positive. Requiring a separator on BOTH
# sides means plain contiguous "911" is left alone here (it doesn't need
# this pre-pass — `\b911\b` already matches it post-normalization) and
# "$9.11" is rejected (no separator between the two "1"s). It also happens
# to reject "9-11-2001"-shaped dates (no separator between the "1"s in
# "11") while still collapsing "9-1-1", "9.1.1", and spelled-with-spaces
# "9 1 1" — all digit-by-digit forms of the emergency number. "/" is
# deliberately NOT in the separator class so slash-formatted dates like
# "9/1/1" are left alone.
_NINE_ONE_ONE_RE: re.Pattern[str] = re.compile(r"\b9[\s.\-]+1[\s.\-]+1\b")


# Unicode dash/hyphen look-alikes -> ASCII hyphen.
#
# NFKD does NOT decompose these to "-" (they're canonical, distinct
# codepoints, not compatibility variants of HYPHEN-MINUS), so without this
# explicit translation a message typed with e.g. U+2011 NON-BREAKING HYPHEN
# ("9‑1‑1") sails through `_fold_unicode` unchanged and `_NINE_ONE_ONE_RE`
# (whose separator class is plain ASCII `[\s.\-]`) never fires.
_UNICODE_DASH_TRANSLATION: dict[int, str] = {
    0x2010: "-",  # HYPHEN
    0x2011: "-",  # NON-BREAKING HYPHEN
    0x2012: "-",  # FIGURE DASH
    0x2013: "-",  # EN DASH
    0x2014: "-",  # EM DASH
    0x2015: "-",  # HORIZONTAL BAR
    0x2212: "-",  # MINUS SIGN
}


def _fold_unicode(text: str) -> str:
    """Fold accented/composed unicode letters to their closest ASCII form,
    and map unicode dash/hyphen look-alikes to ASCII "-".

    ``_PUNCT_RE`` strips anything that isn't ``[a-z0-9\\s]``, which silently
    deletes non-ASCII letters (e.g. "é") rather than folding them, destroying
    keywords like "everywhere" when typed as "éverywhere". NFKD decomposes
    each accented character into a base letter + combining mark(s); dropping
    the combining marks yields the plain ASCII base letter. Stdlib-only
    (``unicodedata``), deterministic, no locale/ICU dependency.

    The dash translation runs AFTER NFKD so "9‑1‑1" (non-breaking hyphen) and
    "9–1–1" (en dash) become plain ASCII "9-1-1" before `_NINE_ONE_ONE_RE`
    (defect #143/#3) ever sees them — and so compatibility codepoints that
    NFKD decomposes *into* one of the translated dashes (e.g. U+FE58 SMALL
    EM DASH → U+2014) are folded too, instead of slipping past a
    translate-first pass.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    dash_folded = decomposed.translate(_UNICODE_DASH_TRANSLATION)
    return "".join(ch for ch in dash_folded if not unicodedata.combining(ch))


def _normalize(text: str) -> str:
    """Fold unicode, lowercase, collapse "9-1-1", strip punctuation, collapse
    whitespace."""
    folded = _fold_unicode(text)
    lowered = folded.lower()
    nine_one_one = _NINE_ONE_ONE_RE.sub("911", lowered)
    no_punct = _PUNCT_RE.sub(" ", nine_one_one)
    return _WS_RE.sub(" ", no_punct).strip()


# ---------------------------------------------------------------------------
# Internal pattern helpers
# ---------------------------------------------------------------------------


def _re(pattern: str) -> re.Pattern[str]:
    """Compile a regex with IGNORECASE (text is already lowercased but
    this is a safety net) and no multiline/dotall — single-message text."""
    return re.compile(pattern, re.IGNORECASE)


# Shared continuous-alarm phrasing alternation — ONE definition, reused by
# all six dedicated continuous-alarm triggers (smoke/fire/CO x fwd/bwd) AND
# as the `refuse_if` veto on the three battery-chirp guard pairs.
#
# Three consecutive safety-review rounds each found a missed synonym in a
# hand-copied variant of this alternation ("sounding", then "won't shut
# off/up", then "will not stop"/"won't turn off"/"quit"/"has not stopped").
# The root cause was six drifting copies; never inline this list again.
# Negation is generalized: `won\s*t` with a zero-width gap also matches the
# apostrophe-stripped "wont", and `will\s+not` covers spelled-out English.
_CONTINUOUS_ALARM_PHRASES: str = (
    r"\b(?:blaring|continuous|nonstop|sounding|ringing|going\s+off|went\s+off"
    r"|(?:won\s*t|will\s+not)\s+(?:stop|shut\s+(?:off|up)|turn\s+off|quit)"
    r"|has\s*n\s*t\s+stopped|has\s+not\s+stopped"
    r"|not\s+stopping|is\s*n\s*t\s+stopping|are\s*n\s*t\s+stopping)\b"
)
_CONTINUOUS_ALARM_RE: re.Pattern[str] = _re(_CONTINUOUS_ALARM_PHRASES)


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
    # "flames" — unambiguous fire hazard word, independent of the bare "fire"
    # anchor. Safety review (#143 finding 2): the fire compound-noun guards
    # (fire escape/extinguisher/pit/hydrant, below) only ever suppress the
    # "fire" token inside their own core phrase span, so a message like
    # "flames shooting out near the fire escape" was silently swallowed
    # because "fire" only appeared inside "fire escape" and "flames" wasn't
    # tracked as its own trigger at all. "flames"/"flame" is never part of
    # any guard's core phrase, so this anchor can never be suppressed.
    _SimpleTrigger("fire", _re(r"\bflames?\b")),
    # "smoke" near smell/filling/everywhere — PROXIMITY trigger anchored on
    # each "smoke" token independently.  This prevents the guard from
    # suppressing a later independent "smoke" that satisfies the condition.
    # Tense/inflection completeness (#144-class defect, found building the
    # #35/#36 eval harness): the proximity word list only had the bare
    # present-tense "smell" — missing "smells"/"smelled"/"smelling" (the SAME
    # verb, other tenses) and "filled" (past tense of "filling"). A tenant
    # saying "I smelled smoke earlier" or "smoke filled the hallway" did not
    # fire. Fixed by completing both verbs' base/3rd-person/past/progressive
    # forms — no new vocabulary, only missing inflections of what was
    # already there.
    *_make_proximity(
        "fire", r"\bsmoke\b", r"(smell|smells|smelled|smelling|filling|filled|everywhere)"
    ),
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
        _re(r"\bsmoke\s+(alarm|detector)\b.{0,30}" + _CONTINUOUS_ALARM_PHRASES),
        suppressible=False,
    ),
    _SimpleTrigger(
        "fire",
        _re(
            _CONTINUOUS_ALARM_PHRASES + r".{0,30}"
            r"\bsmoke\s+(alarm|detector)\b"
        ),
        suppressible=False,
    ),
    # Same continuous-alarm exemption extended to "fire alarm/detector" —
    # discovered while verifying the safety-review CO fix (finding 1):
    # the "fire_alarm_battery" guard (below) has the IDENTICAL structural
    # hole the reviewer flagged for CO — its core span ("fire alarm" /
    # "fire detector") overlaps the bare `\bfire\b` trigger, which IS
    # suppressible by default, so "fire alarm wont stop, checked the
    # battery already" / "fire alarm blaring, might be low battery" /
    # "continuous fire alarm, might need a battery" were silently
    # suppressed (confirmed empirically). "fire alarm going off"/"sounding"
    # happened to still fire via the generic gas_co `alarm going off`/
    # `alarm sounding` triggers (a different category, unrelated to this
    # guard's `protects="fire"`), but "blaring"/"wont stop"/"nonstop"/
    # "continuous" have no such rescue. Fixed proactively, same pattern as
    # smoke/CO above, before this shipped: a continuous fire alarm is
    # EMERGENCY regardless of a co-occurring battery mention.
    _SimpleTrigger(
        "fire",
        _re(r"\bfire\s+(alarm|detector)\b.{0,30}" + _CONTINUOUS_ALARM_PHRASES),
        suppressible=False,
    ),
    _SimpleTrigger(
        "fire",
        _re(
            _CONTINUOUS_ALARM_PHRASES + r".{0,30}"
            r"\bfire\s+(alarm|detector)\b"
        ),
        suppressible=False,
    ),
    # ---------------------------------------------------------------- gas_co
    # "gas" near smell/leak/smells (within 40 chars either direction)
    #
    # CONFIRMED DEFECT (found building the #35/#36 eval harness, #144-class):
    # E2's canonical eval message -- "the kitchen has smelled like gas since
    # I got home an hour ago", lifted verbatim from eval-scenarios-v1.md --
    # did NOT trip Tier-0, even though emergency-prefilter.md mandates E1/E2
    # must. Root cause: the proximity word list had "smell"/"smells" (present
    # tense) and "leak"/"leaking" but never the PAST tense "smelled" at all
    # (confirmed empirically: `prefilter.check("... has smelled like gas
    # ...").hard_hit` was False; `"... smells like gas"` was True). Same
    # hand-copied-alternation-drift lesson as #144: fixed by completing
    # "smell"'s full base/3rd-person/past/progressive set (smell, smells,
    # smelled, smelling) plus "smelt" (British/Canadian spelling variant --
    # equally valid tenant phrasing), and "leak"'s missing past tense
    # (leaked), alongside "leaks" (3rd person) which was also absent.
    *_make_proximity(
        "gas_co",
        r"\bgas\b",
        r"(smell|smells|smelled|smelt|smelling|leak|leaks|leaking|leaked)",
    ),
    # "leak" near "gas" (reverse order proximity) — anchor pattern itself
    # extended the same way: \bleak(s|ing)?\b never matched "leaked" (no
    # word boundary between "leak" and "ed" within the same token).
    *_make_proximity("gas_co", r"\bleak(s|ing|ed)?\b", r"gas"),
    # carbon monoxide (spelled out)
    _SimpleTrigger("gas_co", _re(r"\bcarbon\s+monoxide\b")),
    # CO alarm / CO detector
    _SimpleTrigger("gas_co", _re(r"\bco\s+(alarm|detector)\b")),
    # "alarm/detector going off" or "sounding" — covers CO alarm sounding.
    # suppressible=False (safety review, #143 finding 1 — BLOCKING): the
    # "co_alarm_battery" guard (below) protects "gas_co" and its anchor-token
    # suppression span ("co alarm/detector" or "carbon monoxide
    # alarm/detector") overlaps the START of these matches (both start on
    # the same "alarm"/"detector" token), so a message like "the co alarm is
    # going off, might be low battery" was silently swallowed. Nothing
    # ROUTINE says "alarm going off" or "alarm sounding" — nonstop/sounding
    # is a continuous-alarm condition, exactly like the smoke case above,
    # and must never be silenced by a battery-word mention elsewhere in the
    # message.
    _SimpleTrigger("gas_co", _re(r"\b(alarm|detector)\s+going\s+off\b"), suppressible=False),
    _SimpleTrigger("gas_co", _re(r"\b(alarm|detector)\s+sounding\b"), suppressible=False),
    # Dedicated CO/carbon-monoxide continuous-alarm triggers, modeled
    # exactly on the smoke continuous-alarm pair above. Needed IN ADDITION
    # to the suppressible=False flip above because that flip only covers
    # the literal adjacent phrasing "alarm going off" — it does NOT cover
    # (a) the gap form "the co alarm IS going off" (an extra word between
    # "alarm" and "going off"), or (b) "blaring"/"wont stop"/"continuous"/
    # "nonstop", which have no dedicated CO trigger at all. Both gaps were
    # confirmed misses even after suppressible=False alone.
    # suppressible=False for the same reason as smoke/the flip above: a
    # continuous CO alarm is EMERGENCY regardless of a co-occurring battery
    # word.
    _SimpleTrigger(
        "gas_co",
        _re(r"\b(?:co|carbon\s+monoxide)\s+(alarm|detector)\b.{0,30}" + _CONTINUOUS_ALARM_PHRASES),
        suppressible=False,
    ),
    _SimpleTrigger(
        "gas_co",
        _re(
            _CONTINUOUS_ALARM_PHRASES + r".{0,30}"
            r"\b(?:co|carbon\s+monoxide)\s+(alarm|detector)\b"
        ),
        suppressible=False,
    ),
    # --------------------------------------------------------------- water
    # flood / flooding / flooded — "flooded" (past tense) was missing;
    # "the basement flooded last night" is at least as common a tenant
    # phrasing as "flooding" (#144-class tense-completeness sweep).
    _SimpleTrigger("water", _re(r"\bflood(ing|ed)?\b")),
    # burst pipe (both word orders) — "burst" is already tense-invariant
    # (present and past share the same form), so no completion needed here.
    _SimpleTrigger("water", _re(r"\bburst\s+pipe\b")),
    _SimpleTrigger("water", _re(r"\bpipe\b.{0,20}\bburst\b")),
    # "water" near active-flow words (within 60 chars either direction).
    # "pouring"/"gushing"/"coming through" were progressive-only — missing
    # the simple past ("water poured through the ceiling all night", "water
    # gushed out of the wall", "water came through the ceiling") is the same
    # class of gap as gas_co's "smelled" miss (#144-class tense sweep).
    *_make_proximity(
        "water",
        r"\bwater\b",
        r"(pouring|poured|gushing|gushed|coming\s+through|came\s+through|through\s+the\s+ceiling)",
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
    # overdose / collapsed — strong medical emergency signals.
    # "overdose(d)?" was missing the progressive "overdosing" ("he's
    # overdosing right now") -- same tense-completeness gap as gas_co's
    # "smelled" miss (#144-class sweep). Root "overdos" + e/ed/ing covers
    # overdose/overdosed/overdosing (dropping the "e" before "-ing" is
    # standard English spelling, not a new word).
    _SimpleTrigger("person", _re(r"\boverdos(?:e|ed|ing)\b")),
    # "collapsed" had ONLY the past tense -- base "collapse" ("he might
    # collapse"), 3rd person "collapses", and progressive "collapsing"
    # ("she's collapsing") were all missing entirely, not merely one
    # inflection short. Root "collaps" + e/es/ed/ing (NOT "collapse" +
    # "ing", which would misspell "collapseing" -- English drops the
    # terminal "e" before "-ing", same subtlety as "overdos" + "ing" above;
    # caught by the base-vs-head verification matrix, which found this
    # exact regex mistake before it shipped).
    _SimpleTrigger("person", _re(r"\bcollaps(?:e|es|ed|ing)\b")),
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
#
# ``refuse_if`` (safety review, #143 finding 2): an OPTIONAL whole-message
# veto pattern.  If set and it matches ANYWHERE in the normalized text, the
# guard does not activate AT ALL for this message — it is not recorded in
# ``guards`` and it suppresses nothing, even if ``pattern`` also matched.
# Used by the fixture compound-noun guards (fire escape/extinguisher/pit/
# hydrant): a fixture mention ("the fire escape door is broken") is routine,
# but a fixture mention alongside an independent hazard word ("flames
# shooting out near the fire escape", "smoke pouring out of the fire
# extinguisher cabinet") is not — those messages must reach the classifier
# un-suppressed. This is a global (whole-message) check, not anchor-token
# scoped, because the hazard word is frequently NOT inside the same phrase
# as the fixture (e.g. "flames" and "fire escape" are unrelated tokens).
_FIRE_HAZARD_OVERRIDE_RE: re.Pattern[str] = _re(r"\bflames?\b|\bsmoke\b|\bburning\b|\bon\s+fire\b")


@dataclass(frozen=True)
class _Guard:
    name: str
    pattern: re.Pattern[str]  # Full guard condition (determines activation)
    core_pattern: re.Pattern[str]  # Core phrase span (determines suppression range)
    protects: str
    refuse_if: re.Pattern[str] | None = None  # whole-message veto (see above)


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
        refuse_if=_CONTINUOUS_ALARM_RE,
    ),
    _Guard(
        name="smoke_detector_battery",
        pattern=_re(
            r"\b(battery|chirp|chirping|beep|beeping|low\s+battery)\b.{0,80}\bsmoke\s+(detector|alarm)\b"
        ),
        core_pattern=_re(r"\bsmoke\s+(detector|alarm)\b"),
        protects="fire",
        refuse_if=_CONTINUOUS_ALARM_RE,
    ),
    # Tenants say "fire alarm"/"fire detector" for what is, functionally, a
    # smoke alarm — the battery-chirp guard above only covered the literal
    # "smoke (detector|alarm)" phrase, so "the fire alarm is chirping, needs
    # a new battery" fired the bare `\bfire\b` trigger and rang the phone for
    # a ROUTINE battery chirp. Same core-pattern-anchored suppression as
    # "smoke_detector_battery": only the "fire alarm/detector" tokens are
    # suppressed, never a later independent "fire" (continuous-alarm
    # triggers are suppressible=False regardless and are unaffected).
    #
    # Deliberately narrower than "smoke_detector_battery": this guard's
    # alternation requires the explicit word "battery" (not bare
    # "chirp"/"chirping"/"beep"/"beeping" alone). "fire alarm" is a more
    # ambiguous term than "smoke detector" — it can also refer to a
    # building-wide fire-alarm siren, and an existing regression test
    # ("the smoke detector and the fire alarm keep chirping",
    # TestRegressionBlocking1GuardOverSuppression) establishes that a bare
    # "fire alarm ... chirping" mention with NO battery word must still fire
    # as an independent anchor. Requiring "battery" is the bias rule applied
    # asymmetrically by category: "fire" is higher-stakes than "smoke", so
    # this guard demands stronger evidence (explicit battery mention) before
    # suppressing, while still fixing the confirmed defect (which does
    # mention "battery").
    _Guard(
        name="fire_alarm_battery",
        pattern=_re(r"\bfire\s+(detector|alarm)\b.{0,80}\b(battery|low\s+battery)\b"),
        core_pattern=_re(r"\bfire\s+(detector|alarm)\b"),
        protects="fire",
        refuse_if=_CONTINUOUS_ALARM_RE,
    ),
    _Guard(
        name="fire_alarm_battery",
        pattern=_re(r"\b(battery|low\s+battery)\b.{0,80}\bfire\s+(detector|alarm)\b"),
        core_pattern=_re(r"\bfire\s+(detector|alarm)\b"),
        protects="fire",
        refuse_if=_CONTINUOUS_ALARM_RE,
    ),
    # Same battery-chirp guard extended to CO alarm/detector and the spelled
    # -out "carbon monoxide alarm/detector" — the rubric's battery-chirp
    # ROUTINE case applies to any alarm type, not just smoke. Same "battery"
    # (not bare chirp/beep) requirement as "fire_alarm_battery" above, for
    # the same asymmetric-ambiguity reasoning; no existing test currently
    # exercises a battery-less "co alarm ... chirping" case, but keeping
    # both new guards consistent avoids introducing an unreviewed asymmetry
    # between them.
    #
    # IMPORTANT (safety review, #143 finding 1 — BLOCKING, confirmed via
    # regression against base): this guard's core span ("co alarm/detector"
    # or "carbon monoxide alarm/detector") overlaps the START of the
    # existing "alarm/detector going off"/"sounding" gas_co triggers AND of
    # the dedicated CO continuous-alarm triggers below, because they all
    # anchor on the same "alarm"/"detector" token. A message that mentions
    # a battery ANYWHERE within 80 chars of "co alarm"/"carbon monoxide
    # alarm" — e.g. "carbon monoxide alarm going off, might be a low
    # battery" — would silently suppress the continuous-alarm signal too if
    # those triggers were suppressible (they are not: see
    # `suppressible=False` on the gas_co "going off"/"sounding" triggers and
    # the dedicated CO continuous-alarm pair, both in `_HARD_TRIGGERS`
    # above). This guard therefore can ONLY ever suppress the plain,
    # non-continuous "co alarm"/"carbon monoxide" mention itself — never a
    # continuous-alarm phrasing, regardless of a co-occurring battery word.
    _Guard(
        name="co_alarm_battery",
        pattern=_re(
            r"\b(?:co\s+(?:alarm|detector)|carbon\s+monoxide\s+(?:alarm|detector))\b"
            r".{0,80}\b(battery|low\s+battery)\b"
        ),
        core_pattern=_re(r"\bco\s+(?:alarm|detector)\b|\bcarbon\s+monoxide\s+(?:alarm|detector)\b"),
        protects="gas_co",
        refuse_if=_CONTINUOUS_ALARM_RE,
    ),
    _Guard(
        name="co_alarm_battery",
        pattern=_re(
            r"\b(battery|low\s+battery)\b.{0,80}"
            r"\b(?:co\s+(?:alarm|detector)|carbon\s+monoxide\s+(?:alarm|detector))\b"
        ),
        core_pattern=_re(r"\bco\s+(?:alarm|detector)\b|\bcarbon\s+monoxide\s+(?:alarm|detector)\b"),
        protects="gas_co",
        refuse_if=_CONTINUOUS_ALARM_RE,
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
    # Anchor-token guards for compound nouns containing the literal word
    # "fire" that are NOT the hazard itself. Modeled exactly on "fire drill"
    # above: core == full pattern (a single literal phrase span), so only the
    # "fire" token that is part of THIS phrase is suppressed — a second,
    # independent "fire" token elsewhere in the message (e.g. "there is a
    # fire near the fire escape") still fires because it falls outside this
    # guard's core span.
    #
    # ``refuse_if=_FIRE_HAZARD_OVERRIDE_RE`` (safety review, #143 finding 2 —
    # HIGH): a fixture mention next to an independent hazard word anywhere
    # in the message ("flames shooting out near the fire escape", "smoke
    # pouring out of the fire escape", "the fire extinguisher discharged and
    # everyone is choking on smoke") must NOT be suppressed — the guard
    # refuses to activate at all when flames/smoke/burning/"on fire" is
    # present, regardless of where in the message it appears.
    _Guard(
        name="fire_escape",
        pattern=_re(r"\bfire\s+escape\b"),
        core_pattern=_re(r"\bfire\s+escape\b"),
        protects="fire",
        refuse_if=_FIRE_HAZARD_OVERRIDE_RE,
    ),
    _Guard(
        name="fire_extinguisher",
        pattern=_re(r"\bfire\s+extinguisher\b"),
        core_pattern=_re(r"\bfire\s+extinguisher\b"),
        protects="fire",
        refuse_if=_FIRE_HAZARD_OVERRIDE_RE,
    ),
    _Guard(
        name="fire_pit",
        pattern=_re(r"\bfire\s+pit\b"),
        core_pattern=_re(r"\bfire\s+pit\b"),
        protects="fire",
        refuse_if=_FIRE_HAZARD_OVERRIDE_RE,
    ),
    _Guard(
        name="fire_hydrant",
        pattern=_re(r"\bfire\s+hydrant\b"),
        core_pattern=_re(r"\bfire\s+hydrant\b"),
        protects="fire",
        refuse_if=_FIRE_HAZARD_OVERRIDE_RE,
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
    # "sparked" (past tense) was missing -- the SAME alternation as "leak"
    # below already correctly includes "ed"; "sparks" had drifted out of
    # sync with it (#144-class tense-completeness sweep, found building the
    # #35/#36 eval harness).
    _Soft("sparks", _re(r"\bspark(s|ing|ed)?\b")),
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
      whenever a guard condition matches AND its ``refuse_if`` hazard-token
      veto, if any, does not match anywhere in the message; even when
      hard_hit is True).

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

        # Whole-message hazard-token veto: if present anywhere, this guard
        # does not activate at all — not recorded, suppresses nothing.
        if guard.refuse_if is not None and guard.refuse_if.search(norm):
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
