"""``draft_response`` node (#33) — drafts an SMS reply in the landlord's voice.

Runs after ``classify_severity`` (architecture.md §5 / apps/api/CLAUDE.md's
node layout order) for the urgent/routine branch — the emergency branch
(``emergency_protocol``, #40/#108) is architecture.md's OTHER branch off
``classify_severity``; nothing in this issue's acceptance criteria asks
this node to reproduce the emergency safety-instruction flow (see "Reported
gap: the EMERGENCY safety instruction" below), so it drafts a normal reply
regardless of which severity it's handed.

Voice profile + house rules
------------------------------
``app.agent.prompts.v1.build_draft_system_prompt`` (frozen) injects
``case_context.voice_profile`` (``{tone, samples}``) into the SYSTEM
prompt. House rules, the tenant's message, and the classified severity/
rules/modifier are all DYNAMIC, per-request content — they go in the USER
message, never the frozen system prompt (the same "system is frozen, the
per-request user content is dynamic by nature" distinction
``classify_severity.py`` relies on for its own context injection).

Severity-aware next-step guidance (paid eval gate finding, 2026-07-05)
-----------------------------------------------------------------------
The real gate's E-class/U-class drafts consistently missed two GENERAL,
topic-derived elements the rubric itself implies but the frozen system
prompt (understandably, being generic) never spells out per-severity:

- **URGENT** drafts need exactly ONE relevant self-help check when the
  issue is heating- or appliance/electrical-adjacent (severity-rubric-
  v1.md's own URGENT list: no heat, dead fridge/stove — a breaker trip or
  an unplugged cord are the obvious first things to rule out), PLUS a
  genuinely concrete next-step commitment — a clock time or a tight,
  bounded window, never "soon"/"this week"/a bare "tomorrow morning"
  (plain-language-rules.md rule 4: "concrete over relative ... never
  'soon'"). A security-adjacent issue (broken lock/door/window) gets the
  bounded-window commitment WITHOUT a self-help check — there is nothing
  for a tenant to "self-help" about a compromised lock; the rubric's own
  urgency there is about a fast, bounded repair window instead.
- **EMERGENCY** drafts need an explicit STRUCTURE: the safety instruction
  first (matching whatever rules fired), then briefly what the landlord is
  doing right now, then a concrete bounded next step — tight, no filler.
- **Refusal topic `access_codes`** drafts need to offer the legitimate
  alternative (the tenant can arrange access themselves) alongside the
  deferral, not just redirect to the landlord.

:func:`_urgent_next_step_guidance` derives which topic applies from the
tenant's message text AND ``rules_fired`` (both already available, no new
inputs) using keyword categories lifted from severity-rubric-v1.md's own
URGENT bullet list — never from eval-scenario wording. This is GENERAL
product behavior (every URGENT/EMERGENCY draft gets it, not just the
scenarios that happened to fail), per this task's explicit instruction not
to key guidance to eval text.

Never suggest an oven/stovetop/open flame as a heat source (gate run 5
triage, 2026-07-05) — LIFE-SAFETY class, not a style guideline
--------------------------------------------------------------------------
``e3-noheat-extreme-cold-infant``'s draft suggested "space heaters or the
oven (off) for warmth" — a fire/carbon-monoxide hazard, not a wording
nitpick, regardless of the hedging "(off)" parenthetical (a tenant reading
quickly could still misread it as an instruction to use the oven). Fixed
at THREE layers, matching this module's existing severity-aware-guidance +
hard-guard pattern:

1. **Topic-derived guidance** (:data:`_UNSAFE_HEAT_SOURCE_GUIDANCE`),
   appended to BOTH the EMERGENCY and URGENT paths whenever the topic is
   heating-related (``_HEATING_TOPIC_RE``) — general product behavior, not
   an eval-scenario-keyed patch, per this module's own established
   convention above. Explicitly: never oven/stovetop/open flame; safe
   alternatives only (a space heater if the tenant has one, blankets, warm
   clothing/layers, and offering to help arrange somewhere warmer to stay
   if the cold is severe).
2. **Hard guard** (:data:`_UNSAFE_HEAT_SOURCE_RE` in :func:`_check_hard_
   guards`): the model's OWN acknowledgment is REJECTED and regenerated if
   it mentions oven/stovetop/gas range/open flame/candles anywhere near a
   warmth/heat word, hedged or not -- this is deterministic and
   code-enforced, not left to the model's own compliance, exactly because
   it is a safety class (see this module's own "Hard guards" section
   below for why guards exist at all: "false positives here cost one extra
   regeneration; false negatives are the failure mode that matters").
3. The appliance/electrical branch of :func:`_urgent_next_step_guidance`
   was ALSO reworded while touching this code (see "One-question hard cap"
   below) to stop suggesting "or"-joined alternatives that read as two
   separate questions.

Concrete-time commitment extended to ROUTINE (gate run 5 triage,
2026-07-05)
--------------------------------------------------------------------------
``r1-faucet-drip``'s draft said "this week" / "soon" — the generic
``_PLAIN_LANGUAGE_REMINDER`` already states "concrete over relative...
never 'soon'" but, unlike EMERGENCY/URGENT, ROUTINE received no
topic-specific reinforcement of it at the point the model commits to a
next step. ``_build_user_content`` now emits the same concrete-next-step
reminder for ROUTINE too (plain-language-rules.md rule 4 is unconditional
-- "concrete over relative" is not an EMERGENCY/URGENT-only rule).

One-question hard cap (gate run 5 triage, 2026-07-05)
--------------------------------------------------------------------------
``u2-fridge-dead``'s draft asked two questions ("Can you check the plug...
or if a breaker tripped?") despite ``_PLAIN_LANGUAGE_REMINDER`` already
saying "at most ONE question" -- traced to the appliance/electrical
next-step guidance ITSELF modeling the failure: it offered its self-help
example as two "or"-joined alternatives ("whether it's plugged in
properly, or whether a breaker has tripped"), which reads as license to
ask about both separately. Fixed by (a) rewording that example as ONE
non-question checklist instruction instead of an "or" question, and (b)
restating the reminder itself as an explicit HARD CAP with the same
concrete failure pattern spelled out, so the model can't miss it.

EMERGENCY: numbered-list safety instructions (gate run 5 finding,
2026-07-05)
--------------------------------------------------------------------------
``e1-water-electrical`` kept hard-failing the judge's must-include/must-
not-include checks even after the length fix landed (its draft was ~260
chars, well under budget, ``length_over_budget=false`` — so "too long" was
NOT the actual cause, contrary to the initial hypothesis; investigated
against the recorded draft body + judge reasoning directly, per this
round's instruction). The draft crammed 5-6 distinct instructions
("turn off power... stay away from the water... don't touch the ceiling
light... calling a plumber... 9am tomorrow... text me if it gets worse")
into ONE dense run-on paragraph. ``docs/02-product/plain-language-rules.md``
rule 2 — a requirement this module's EMERGENCY guidance had never actually
encoded — is explicit: **"In an emergency, instructions come as a numbered
list, most important first, max three steps."** :data:`_EMERGENCY_
STRUCTURE_GUIDANCE` now spells this out literally (numbered list, most-
important-first, at most 3 steps, one action per line, no piggy-backed
reassurance on a safety line) — a genuine, previously-missed, doc-mandated
requirement, not a scenario-specific patch; every EMERGENCY draft gets it,
regardless of which rules fired. Separating each instruction onto its own
numbered line is also, independently, the most direct way to make "does
this draft include X" unambiguous to ANY reader (human or judge) — a dense
paragraph is exactly the shape that makes a specific sentence hard to
isolate.

Refusal-deferral templates: code APPENDS, the model never weaves them in
(deferral architecture ruling, senior review 2026-07-05)
-------------------------------------------------------------------------
An earlier revision asked the model to "include the deferral language
naturally" in its own reply — but the frozen system prompt's plain-
language guidance (concise, no repeated boilerplate) pushed AGAINST
verbatim reproduction, so a model that PARAPHRASED a refusal topic (e.g.
"about the rent discount you mentioned — Laith will sort that out") still
carried enough of the same vocabulary ("discount") to trip its own guard
under the exact-string whitelist alone — degrading a perfectly safe reply
to the generic fallback purely for topical wording, not an actual
violation.

Fixed by SEPARATING drafting from deferral entirely:

1. The DYNAMIC user content (see ``_build_user_content``) tells the model
   NOT to address a flagged topic at all — a short, warm acknowledgment of
   the tenant's message ONLY, never a policy explanation, and explicitly
   NOT to write, quote, or paraphrase any deferral language itself (a
   separate note is appended automatically). Because the appended template
   ALREADY tells the tenant the topic was passed to the landlord for
   direct follow-up, the model is told NOT to say "passed it along" / "will
   follow up" itself and NOT to sign off ("talk soon") — eval gate 6
   (2026-07-05) showed the combined draft otherwise reads as two disjoint
   texts glued together (hand-off stated twice, sign-off mid-message),
   which the judge correctly failed against the plain-language rules.
2. The model's OWN acknowledgment text is guard-checked (see "Hard
   guards" below) — REJECTED and regenerated once if it violates, exactly
   as before.
3. Once accepted (or replaced by :data:`_GENERIC_SAFE_FALLBACK` after a
   second violation), THIS NODE appends the canned
   ``app.agent.prompts.v1.REFUSAL_TEMPLATES`` deferral text for every flag
   on the classification — verbatim, by construction
   (:func:`_append_deferrals`). The model never generates, never
   paraphrases, and is never asked to reproduce this text; there is
   nothing for it to get wrong once the code itself writes it.

``_strip_mandated_templates`` is kept as defense-in-depth: it scrubs any
mandated template text out of the MODEL's own acknowledgment before guard
-checking, in case a model quotes/paraphrases a topic despite being told
not to.

Hard guards — post-generation, code-enforced (never LLM-trusted alone)
---------------------------------------------------------------------------
Run against the MODEL's OWN acknowledgment text only (before this node
appends any deferral — see above), with every mandated
``REFUSAL_TEMPLATES`` string stripped out first
(:func:`_strip_mandated_templates`). Three categories:

- **Dollar amounts / compensation PROMISES**: an explicit amount
  ("$50"), a percentage/relative offer ("20% off", "half off your rent"),
  or a compensation WORD ("reimburse", "refund", "waive", "discount",
  "rent reduction/abatement") paired with a first-person COMMITMENT
  ("I'll", "I can", "we'll", "you'll get") within a short window and with
  NO negation between them (:func:`_has_compensation_commitment`). Bare
  topical vocabulary alone — "about the rent discount you mentioned" with
  no commitment attached, or "I can't discuss compensation" (a negated
  commitment) — is deliberately NOT a violation (safety review,
  2026-07-05): a message that merely acknowledges a topic exists, or
  correctly refuses to engage it, must never be punished for the
  vocabulary alone.
- **Access/door codes or PINs**: an explicit code/PIN value, or
  non-numeric disclosure (a key/lockbox/spare LOCATED somewhere — "the
  lockbox is under the mat").
- **Legal positions**: LTB/eviction/entitlement claims, and indirect/
  negative framings ("our lawyer says", "you have no right to", "you
  don't have a case"). Known limitation, not fixed this round (no test
  demands it and the bare terms are comparatively rare/specific): a
  neutral acknowledgment that happens to NAME the topic ("I've noted your
  eviction question") could still trip this guard the same way the
  dollar-guard used to — flagged here rather than silently assumed fixed.

A violating draft is REJECTED and regenerated exactly ONCE, with the
violation named in the retry's USER-message suffix (never the system
prompt — frozen). A SECOND violation (of any kind, including a repeated
Anthropic API failure) replaces the model's acknowledgment with
:data:`_GENERIC_SAFE_FALLBACK` — ``state["draft_guard_failed"] = True`` is
set in that case, the same seam pattern
``app/agent/nodes/classify_severity.py`` uses for
``classification_failed``. Either way, the canned deferral(s) are STILL
appended afterward (see above) — guard failure is now purely about
whether the model's OWN acknowledgment was safe, never about whether the
deferral made it into the draft (the code guarantees that
unconditionally, regardless of guard outcome).

**v1 pattern-coverage, not the authoritative gate:** these are
deterministic substring/regex checks, not semantic understanding of the
draft — a fast, always-on backstop so a violation is caught and retried/
replaced BEFORE a landlord ever sees the draft. ``#35``'s eval grader
(LLM-as-judge + its own substring assertions) is the AUTHORITATIVE check
for "did this draft actually violate a hard rule"; these guards are the
first line of defense that ships with this issue, not a replacement for
that grader.

Length discipline: regenerate once, then flag — TRUNCATION IS FORBIDDEN
(paid eval gate finding, 2026-07-05)
-------------------------------------------------------------------------
``docs/02-product/plain-language-rules.md`` rule 5: routine/urgent/
emergency drafts get a ~300-char budget (:data:`_LENGTH_BUDGET_CHARS`);
refusal-topic drafts are the documented exception (see below) because the
mandated deferral can legitimately push them longer. The real gate's
E-class drafts blew this budget (380/377 chars) even with NO refusal
topic involved — a plain quality miss, not a safety one, but still worth
fixing deterministically rather than hoping the model self-polices:

1. **Proactive**: ``_build_user_content`` always tells the model its
   available character budget (:func:`_available_ack_chars`) — accounting
   for the length of whatever deferral text WILL be appended afterward, so
   the model's own portion is asked to leave room for it, even though the
   post-check below never enforces the combined length when a deferral is
   present (see point 3).
2. **Reactive**: the SAME 2-attempt loop that already retries on a hard
   -guard violation ALSO retries once when the model's (guard-clean)
   acknowledgment exceeds the budget (:func:`_length_retry_note`) — one
   extra regeneration, not a second independent budget on top of the
   shared 20s deadline (see "20s END-TO-END budget" below).
3. **Documented exception**: a message with any refusal flag is EXEMPT
   from the length check entirely (mirrors the guard-side "Plain-language
   exception" this module already had) — correctness (never omitting the
   mandated deferral) outweighs segment-length there.
4. **Truncation is forbidden.** If the guard-clean acknowledgment is STILL
   over budget after the one regeneration attempt (or the retry was
   skipped because the shared deadline ran out), THIS NODE NEVER CUTS THE
   TEXT. The long draft is kept exactly as generated, and
   ``state["length_over_budget"] = True`` is set — a landlord-review
   signal ("you can shorten this before it sends"), completely
   independent of ``draft_guard_failed`` (a draft can be guard-clean but
   long, or short but guard-violating — two different dimensions, tracked
   separately, see the post-loop decision logic in :func:`draft_response`).

Cost accounting (audit_log 'drafted' payload)
--------------------------------------------------
Every real Anthropic call this node makes (the initial attempt AND any
regeneration, whether guard- or length-driven) contributes its
``tokens_in``/``tokens_out`` to a running total — every call costs real
money even when its output is rejected. ``model`` records the last call's
reported model id (``None`` if every attempt failed at the transport level
with no response at all). All four (``model``, ``tokens_in``,
``tokens_out``, ``cost_cents``) are added to the existing ``'drafted'``
``audit_log`` payload alongside ``draft_id``, ``refusal_templates_used``,
and ``guard_failed`` — never a message body.

Reported gap: the EMERGENCY safety instruction
--------------------------------------------------
``prompts/v1.py``'s frozen draft system prompt says: "For EMERGENCY
severity: prepend the mandatory safety instruction (also provided
separately) before any other content." No canned safety-instruction text
exists ANYWHERE in this codebase yet (no per-category template comparable
to ``REFUSAL_TEMPLATES``) — the actual safety SMS to the tenant is
``app/agent/emergency.py``'s #108 seam, which is still a no-op stub today.
This node does NOT fabricate that text; the dynamic-content STRUCTURE
guidance above (safety first, then landlord status, then a concrete next
step) tells the model how to organize its OWN reply, but the model is
still the one writing the actual safety content from ``rules_fired`` —
there is no separate, pre-approved safety template being injected.
Flagged in the issue report for the spec owner — likely #108's
deliverable, not #33's.

Reported gap: ``drafts.status`` vocabulary vs. the issue text
-----------------------------------------------------------------
Issue #33 says "Draft stored with status awaiting_approval" — but
``schema-v1.md``'s ``drafts.status`` CHECK is
``('pending','stale','approved','sending','sent','rejected','cancelled')``
— there is no ``awaiting_approval`` value for ``drafts``; that value
belongs to ``cases.status`` instead. This node follows the SCHEMA: new
drafts are inserted with ``status='pending'`` (the CASE separately moves to
``awaiting_approval`` — a case-status transition this issue does not own;
no code here touches ``cases.status``). Flagged as a discrepancy between
the issue text and schema-v1.md, per this task's instructions.

Stale-then-insert (conversation-model.md's stale-draft rule)
------------------------------------------------------------------
"a case has at most one pending draft" (the ``uq_drafts_one_pending``
partial unique index). If a pending draft already exists for this case
when a new draft is produced, it is marked ``stale`` FIRST (audit_log
``draft_stale``, existing vocabulary), then the new ``pending`` row is
inserted — both within the same DB transaction, so the two ``pending``
rows never coexist even momentarily. This is conversation-model.md's own
documented rule ("new inbound → old draft stale → re-run"), implemented
minimally here (no notification, no diffing of what changed — that is
#44/#50 territory per the same doc).

20 s END-TO-END budget / retry
----------------------------------
Same shared-deadline arithmetic as ``classify_intent.py`` /
``classify_severity.py`` (``app/integrations/anthropic.py``'s
``new_deadline`` / ``attempt_timeout``): ONE 20-second deadline for the
initial attempt and its single regeneration TOGETHER, not 20 seconds each.
The regeneration attempt here is triggered by EITHER a transport failure,
a hard-guard violation, OR a length-budget violation (see "Hard guards" /
"Length discipline" above) — whichever fires first on a given attempt;
either way it draws from the SAME shared deadline. A regeneration that
would fall below the 2-second floor is skipped entirely: a guard violation
in that state falls back to the safe generic draft
(``draft_guard_failed=True``); a pure length violation (guards were clean)
keeps the long draft as generated (``length_over_budget=True``) — see the
post-loop decision logic in :func:`draft_response`.

DB access
---------
Admin engine, same pattern as the other #30/#110/#32 nodes: one session to
read the message body + tenant name, the Anthropic call(s) made OUTSIDE
any open session (mirrors ``classify_severity.py``/``load_context.py``'s
"never hold a pooled connection across a slow external call"), then a
second session for the stale-then-insert drafts/audit writes. Allowlisted
in ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.

Never-break rule #5: only uuids, guard names, and boolean flags ever reach
``log.*`` calls in this module — never the tenant's message body or the
drafted reply text.
"""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from app.agent.prompts.v2 import (
    PROMPT_VERSION,
    REFUSAL_TEMPLATES,
    build_draft_system_prompt,
    get_refusal_deferral,
)
from app.agent.schemas import CaseContext, DraftResult, RefusalFlag, Severity, SeverityResult
from app.agent.state import AgentState
from app.agent.tools import DRAFT_MESSAGE_TOOL
from app.db.session import get_admin_session
from app.integrations import anthropic as anthropic_mod

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Node-local fallback template — see module docstring "Refusal-deferral
# templates". Rule-#8-clean copy: short, plain, no jargon.
# ---------------------------------------------------------------------------

# Two sentences: copy-guardian (2026-07-06) counted the em-dash version as
# one 19-word sentence, over plain-language rule 1's 15-word cap.
_GENERIC_SAFE_FALLBACK: str = (
    "Thanks for letting me know. I've passed this along to your landlord, who will "
    "follow up with you directly."
)

# ---------------------------------------------------------------------------
# Dynamic (non-frozen) plain-language reminder — injected into the USER
# message every time, per docs/02-product/plain-language-rules.md. Not a
# prompt-file edit: this is per-request content, exactly like the tenant
# message itself.
# ---------------------------------------------------------------------------

_PLAIN_LANGUAGE_REMINDER: str = (
    "Writing rules for this reply (docs/02-product/plain-language-rules.md):\n"
    "- Grade-5 reading level: short words, short sentences, active voice.\n"
    '- No jargon or idioms (no "touch base", no "ASAP").\n'
    '- Concrete over relative: a specific day and time window, never "soon" or "later '
    'this week".\n'
    "- HARD CAP: at most ONE question mark in the whole reply, covering at most ONE "
    'question. Never join two checks with "or" and a question mark (e.g. "is it '
    'plugged in, or did a breaker trip?" is TWO questions) -- ask ONE, or turn '
    'multiple checks into a single non-question instruction instead (e.g. "check '
    "that it's plugged in and the breaker hasn't tripped\").\n"
    "- Routine replies: 2 SMS segments or fewer (about 300 characters).\n"
    "- Calm, warm, certain tone — never scolding, never panicked."
)

# ---------------------------------------------------------------------------
# Severity-aware next-step guidance (see module docstring). Topic keywords
# lifted from severity-rubric-v1.md's own URGENT bullet list, never from
# eval-scenario text -- this is general product behavior.
# ---------------------------------------------------------------------------

_SECURITY_TOPIC_RE = re.compile(r"\b(?:lock|deadbolt|door|window|latch)\w*\b", re.IGNORECASE)
_HEATING_TOPIC_RE = re.compile(r"\b(?:heat|heater|furnace|thermostat)\w*\b", re.IGNORECASE)
_APPLIANCE_ELECTRICAL_TOPIC_RE = re.compile(
    r"\b(?:fridge|refrigerator|freezer|oven|stove|outlet|plug(?:ged|ging)?|appliance|"
    r"power|electrical|breaker)\w*\b",
    re.IGNORECASE,
)

_CONCRETE_NEXT_STEP_GUIDANCE: str = (
    'a specific, bounded next step -- a clock time (e.g. "9am tomorrow") or a tight '
    'window (e.g. "within 24 hours"), never a vague word like "soon", "this week", or '
    'a bare "tomorrow morning" with no time attached'
)


_UNSAFE_HEAT_SOURCE_GUIDANCE: str = (
    "Never suggest using an oven, stovetop, gas range, or any open flame/candle as a "
    "heat source -- this is a fire/carbon-monoxide safety hazard, not a style choice, "
    'even hedged (e.g. do not write anything like "the oven (off) for warmth"). If '
    "warming guidance is needed, offer ONLY safe alternatives: a space heater if the "
    "tenant has one, extra blankets, warm clothing/layers, and -- if the cold is "
    "severe -- offer to help arrange somewhere warmer to stay."
)


def _urgent_next_step_guidance(topic_text: str) -> str:
    """Topic-derived next-step guidance for URGENT severity — see module
    docstring "Severity-aware next-step guidance" / "Never suggest an
    oven/stovetop/open flame..." / "One-question hard cap". *topic_text*
    is the tenant message plus the classifier's own ``rules_fired`` text
    (both already available to this node; no new inputs)."""
    if _SECURITY_TOPIC_RE.search(topic_text):
        return (
            "This is a security issue (lock/door/window). Commit to "
            f"{_CONCRETE_NEXT_STEP_GUIDANCE} for the repair -- a compromised lock "
            "needs a fast, bounded window, not a self-help check."
        )
    if _HEATING_TOPIC_RE.search(topic_text):
        return (
            "Include ONE quick self-help check the tenant can try right now (e.g. "
            "checking the thermostat setting and the breaker) -- state it as a "
            'SINGLE non-question instruction, not two "or"-joined questions. This is '
            f"a safety requirement, not a style choice: {_UNSAFE_HEAT_SOURCE_GUIDANCE} "
            f"AND commit to {_CONCRETE_NEXT_STEP_GUIDANCE}."
        )
    if _APPLIANCE_ELECTRICAL_TOPIC_RE.search(topic_text):
        return (
            "Include ONE self-help step the tenant can try right now, relevant to "
            "this issue, stated as a SINGLE non-question instruction, not two "
            '"or"-joined questions (e.g. "check that it\'s plugged in and the '
            'breaker hasn\'t tripped" -- one sentence, one instruction, not "is it '
            'plugged in, or did a breaker trip?") -- AND commit to '
            f"{_CONCRETE_NEXT_STEP_GUIDANCE}."
        )
    return f"Commit to {_CONCRETE_NEXT_STEP_GUIDANCE}."


_ROUTINE_NEXT_STEP_GUIDANCE: str = f"Commit to {_CONCRETE_NEXT_STEP_GUIDANCE}."
"""Plain-language-rules.md rule 4 ("concrete over relative") is
unconditional, not an EMERGENCY/URGENT-only rule -- see module docstring
"Concrete-time commitment extended to ROUTINE"."""

_EMERGENCY_STRUCTURE_GUIDANCE: str = (
    "Structure this EMERGENCY reply in order: (1) the safety instruction(s) first -- "
    "the concrete thing(s) the tenant should do right now, matching the rules that "
    "fired above. Per docs/02-product/plain-language-rules.md rule 2, format these as "
    'a short NUMBERED LIST ("1. ...", "2. ...") -- most important step first, AT MOST '
    "3 steps, ONE distinct action per line -- never combine two actions on one line and "
    "never pad a step with extra reassurance or explanation; (2) then, on its own line "
    "(not part of the numbered list), briefly what you (the landlord) are doing right "
    "now; (3) then, on its own line, commit to "
    f"{_CONCRETE_NEXT_STEP_GUIDANCE}. Keep it tight -- no filler, no repeated "
    "reassurance, nothing beyond these lines."
)

_ACCESS_ALTERNATIVE_GUIDANCE: str = (
    "For the access-code topic specifically: besides the brief acknowledgment, mention "
    "that the tenant can arrange this themselves directly (e.g. meeting the person in "
    "person, or another arrangement that doesn't need a code from you) -- a legitimate "
    "alternative to relying on you for access, not a workaround for sharing the code "
    "itself."
)

# ---------------------------------------------------------------------------
# Hard guards — post-generation regex checks (see module docstring).
# Deliberately conservative: false positives here cost one extra
# regeneration; false negatives are the failure mode that matters. The
# compensation check additionally requires a nearby, non-negated
# first-person COMMITMENT (see _has_compensation_commitment) so bare
# topical vocabulary alone never trips it (safety review, 2026-07-05).
# ---------------------------------------------------------------------------

_DOLLAR_AMOUNT_RE = re.compile(r"\$\s?\d")
_PERCENT_OFF_RE = re.compile(r"\b\d{1,3}\s?%\s*off\b", re.IGNORECASE)
_HALF_OFF_RE = re.compile(r"\bhalf\s+off\b", re.IGNORECASE)

_COMPENSATION_WORD_RE = re.compile(
    r"\bcompensat(?:e|ion|ing)\b"
    r"|\breimburs(?:e|ement|ing)\b"
    r"|\brefund(?:s|ed|ing)?\b"
    r"|\brent\s+(?:reduction|abatement)\b"
    r"|\bwaive(?:d|s|r)?\b"
    r"|\bdiscount(?:s|ed|ing)?\b",
    re.IGNORECASE,
)

_COMMITMENT_PHRASE_RE = re.compile(
    r"\bi(?:'ll|\s+will|\s+can)\b"
    r"|\bwe(?:'ll|\s+will|\s+can)\b"
    r"|\byou(?:'ll|\s+will)\s+(?:get|receive)\b",
    re.IGNORECASE,
)

# Deliberately broad, substring-level "n't" match (not word-bounded) so it
# catches every English contraction (can't, won't, wouldn't, ...) without
# enumerating them; a bare "not"/"never" token is also a negation.
_NEGATION_RE = re.compile(r"\bnot\b|n't|\bnever\b", re.IGNORECASE)

_COMPENSATION_PROXIMITY_WINDOW_CHARS = 40


def _has_compensation_commitment(text: str) -> bool:
    """True when a compensation WORD appears near a first-person
    commitment phrase with NO negation between them — see module docstring
    "Hard guards" for why bare topical vocabulary alone (no commitment, or
    a NEGATED commitment like "I can't discuss compensation") is
    deliberately NOT a violation."""
    for comp_match in _COMPENSATION_WORD_RE.finditer(text):
        start = max(0, comp_match.start() - _COMPENSATION_PROXIMITY_WINDOW_CHARS)
        end = min(len(text), comp_match.end() + _COMPENSATION_PROXIMITY_WINDOW_CHARS)
        window = text[start:end]
        if _COMMITMENT_PHRASE_RE.search(window) and not _NEGATION_RE.search(window):
            return True
    return False


_ACCESS_CODE_RE = re.compile(
    r"\b(?:access|door|gate|lock\s*box|entry|building|keypad|garage)\s*codes?\b\D{0,20}\d{2,}"
    r"|\bcodes?\s*(?:is|:|are)\s*\d{2,}"
    r"|\bpin\s*(?:is|:)\s*\d{2,}"
    # Non-numeric access disclosure — a key/lockbox/spare LOCATED somewhere,
    # either word order ("the lockbox is under the mat" / "hidden behind
    # the key... spare"). No digits required — the location itself is the
    # leak.
    r"|\b(?:key|lockbox|lock\s*box|spare\s*key|spare)\b.{0,30}\b(?:under|behind|hidden|"
    r"beneath|inside|on\s*top\s*of)\b"
    r"|\b(?:under|behind|hidden|beneath|inside|on\s*top\s*of)\b.{0,30}\b(?:the\s+)?"
    r"(?:key|lockbox|lock\s*box|spare\s*key|spare)\b",
    re.IGNORECASE,
)

_LEGAL_POSITION_RE = re.compile(
    r"\bltb\b"
    r"|\blandlord\s+and\s+tenant\s+board\b"
    r"|\beviction\b"
    r"|\byou(?:'re| are)\s+entitled\s+to\b"
    r"|\blegally\s+(?:required|obligated|entitled)\b"
    r"|\b(?:violat(?:es|ion)|against)\s+the\s+law\b"
    # Indirect/negative legal positions — still a substantive legal opinion,
    # just phrased as a third-party claim or a denial of rights.
    r"|\bour\s+lawyer\s+says\b"
    r"|\byou\s+have\s+no\s+right\s+to\b"
    r"|\byou\s+don'?t\s+have\s+a\s+case\b",
    re.IGNORECASE,
)

# Oven/stovetop/open-flame-as-heat-source — LIFE-SAFETY class, not style
# (see module docstring "Never suggest an oven/stovetop/open flame...").
# Deliberately fires regardless of hedging ("the oven (off) for warmth" still
# matches) -- a suggestion this hazardous is rejected outright, never
# "mostly safe because it says off". Window-based like every other guard in
# this file (see "Hard guards" docstring above for why false positives here
# are an accepted, low-cost tradeoff).
_UNSAFE_HEAT_SOURCE_RE = re.compile(
    r"\b(?:oven|stove\s*-?\s*top|stovetop|gas\s+range|open\s+flame|candles?)\b.{0,50}"
    r"\b(?:warm(?:th|ing|er|ed)?|heat(?:ing|ed)?)\b"
    r"|\b(?:warm(?:th|ing|er|ed)?|heat(?:ing|ed)?)\b.{0,50}"
    r"\b(?:oven|stove\s*-?\s*top|stovetop|gas\s+range|open\s+flame|candles?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Length budget (see module docstring "Length discipline").
# ---------------------------------------------------------------------------

_LENGTH_BUDGET_CHARS: int = 300
"""plain-language-rules.md rule 5: "routine replies ≤2 SMS segments (~300
chars)" — applied uniformly to routine/urgent/emergency drafts (the doc's
separate "≤3 short numbered lines" for "emergency safety messages" refers
to the #108 safety-SMS template, a different artifact than this node's own
approval-gated follow-up reply — see "Reported gap: the EMERGENCY safety
instruction"). Refusal-topic drafts are exempt entirely, see
:func:`_available_ack_chars` / the module docstring "Documented
exception"."""

_MIN_ACK_CHARS_FLOOR: int = 60
"""Even the longest REFUSAL_TEMPLATES entry (or several stacked -- the
budget math in :func:`_available_ack_chars` measures the actual appended
text) must never drive the model's suggested acknowledgment budget down to
something that can't fit a single short sentence."""


def _combined_deferral_text(refusal_flags: list[RefusalFlag]) -> str:
    """The exact text :func:`_append_deferrals` will append — joined, not
    yet prefixed with the separating space. Empty string when there are no
    refusal flags."""
    if not refusal_flags:
        return ""
    return " ".join(get_refusal_deferral(flag.value) for flag in refusal_flags)


def _available_ack_chars(refusal_flags: list[RefusalFlag]) -> int:
    """How many characters the model's OWN acknowledgment should aim for,
    accounting for the deferral text that will be appended afterward (see
    module docstring "Length discipline", point 1) — a proactive hint, not
    an enforced cap (enforcement is skipped entirely when refusal_flags is
    non-empty; see :data:`_LENGTH_BUDGET_CHARS`'s "Documented exception")."""
    deferral_text = _combined_deferral_text(refusal_flags)
    if not deferral_text:
        return _LENGTH_BUDGET_CHARS
    return max(_MIN_ACK_CHARS_FLOOR, _LENGTH_BUDGET_CHARS - len(deferral_text) - 1)


def _length_retry_note(available_chars: int) -> str:
    return (
        "\n\nIMPORTANT: your previous reply was too long. Shorten it to under "
        f"{available_chars} characters total (every character counts on a phone "
        "screen) while keeping the key information. Revise and resend the FULL reply."
    )


_SELECT_MESSAGE_SQL = text("SELECT body FROM messages WHERE id = :message_id")
_SELECT_TENANT_NAME_SQL = text("SELECT name FROM tenants WHERE id = :tenant_id")

_SELECT_PENDING_DRAFT_SQL = text(
    "SELECT id FROM drafts WHERE case_id = :case_id AND status = 'pending'"
)
_UPDATE_DRAFT_STALE_SQL = text(
    "UPDATE drafts SET status = 'stale', updated_at = now() WHERE id = :draft_id"
)
_INSERT_DRAFT_SQL = text(
    "INSERT INTO drafts (landlord_id, case_id, recipient, body, prompt_version, status) "
    "VALUES (:landlord_id, :case_id, 'tenant', :body, :prompt_version, 'pending') "
    "RETURNING id"
)
_INSERT_DRAFT_STALE_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'draft_stale', CAST(:payload AS jsonb))"
)
_INSERT_DRAFTED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'drafted', CAST(:payload AS jsonb))"
)


def _strip_mandated_templates(body: str) -> str:
    """Remove every exact ``REFUSAL_TEMPLATES`` string from *body* before the
    hard guards ever see it — defense-in-depth (see module docstring
    "Refusal-deferral templates"): under the current architecture the model
    is told never to write this text at all (the code appends it
    afterward), but a model that quotes/paraphrases it anyway must not have
    that mandated text held against it.
    """
    scrubbed = body
    for template_text in REFUSAL_TEMPLATES.values():
        scrubbed = scrubbed.replace(template_text, " ")
    return scrubbed


def _check_hard_guards(*, body: str) -> list[str]:
    """Return the list of violated guard names for the MODEL's OWN
    acknowledgment text — empty means clean. See module docstring "Hard
    guards" for the full rationale of each category."""
    scrubbed_body = _strip_mandated_templates(body)
    violations: list[str] = []
    if (
        _DOLLAR_AMOUNT_RE.search(scrubbed_body)
        or _PERCENT_OFF_RE.search(scrubbed_body)
        or _HALF_OFF_RE.search(scrubbed_body)
        or _has_compensation_commitment(scrubbed_body)
    ):
        violations.append("dollar_compensation")
    if _ACCESS_CODE_RE.search(scrubbed_body):
        violations.append("access_code")
    if _LEGAL_POSITION_RE.search(scrubbed_body):
        violations.append("legal_position")
    if _UNSAFE_HEAT_SOURCE_RE.search(scrubbed_body):
        violations.append("unsafe_heat_source")
    return violations


def _violation_retry_note(violations: list[str]) -> str:
    return (
        "\n\nIMPORTANT: your previous reply violated the following hard rule(s): "
        f"{', '.join(violations)}. Do not include dollar amounts, compensation promises, "
        "reimbursement/refund/discount language, access codes or PINs, any suggestion to "
        "use an oven, stovetop, or open flame for warmth/heat (safe alternatives only: a "
        "space heater if available, blankets, warm clothing, or offering to help arrange "
        "somewhere warmer to stay), or any legal "
        "position (LTB, eviction, entitlement claims). Revise and resend the FULL reply."
    )


def _append_deferrals(ack_body: str, refusal_flags: list[RefusalFlag]) -> str:
    """Append the canned ``REFUSAL_TEMPLATES`` deferral(s), verbatim, for
    every flag on the classification — code-appended, never model-
    generated (see module docstring "Refusal-deferral templates"). A no-op
    when there are no refusal flags."""
    deferral_text = _combined_deferral_text(refusal_flags)
    if not deferral_text:
        return ack_body
    return f"{ack_body.rstrip()} {deferral_text}"


def _build_user_content(
    *,
    body: str,
    tenant_name: str | None,
    house_rules: str | None,
    severity_result: SeverityResult,
    refusal_flags: list[RefusalFlag],
) -> str:
    lines: list[str] = [f"Tenant's message:\n{body}", ""]
    if tenant_name:
        lines.append(f"Tenant's first name: {tenant_name}")
    lines.append(f"Classified severity: {severity_result.severity.value}")
    if severity_result.rules_fired:
        lines.append(f"Rules that fired: {'; '.join(severity_result.rules_fired)}")
    if severity_result.modifier:
        lines.append(f"Modifier applied: {severity_result.modifier}")
    if house_rules:
        lines.append(f"\nProperty house rules (use only what's relevant):\n{house_rules}")

    # Severity-aware structural guidance -- see module docstring
    # "Severity-aware next-step guidance". Topic derived from the message +
    # rules_fired, never from eval-scenario wording.
    if severity_result.severity is Severity.EMERGENCY:
        lines.append(f"\n{_EMERGENCY_STRUCTURE_GUIDANCE}")
        topic_text = f"{body} {' '.join(severity_result.rules_fired)}"
        if _HEATING_TOPIC_RE.search(topic_text):
            lines.append(f"\n{_UNSAFE_HEAT_SOURCE_GUIDANCE}")
    elif severity_result.severity is Severity.URGENT:
        topic_text = f"{body} {' '.join(severity_result.rules_fired)}"
        lines.append(f"\n{_urgent_next_step_guidance(topic_text)}")
    elif severity_result.severity is Severity.ROUTINE:
        lines.append(f"\n{_ROUTINE_NEXT_STEP_GUIDANCE}")

    if refusal_flags:
        topics = ", ".join(flag.value.replace("_", " ") for flag in refusal_flags)
        lines.append(
            f"\nThis message touches on a topic the landlord handles directly, not you: "
            f"{topics}. Do NOT explain, negotiate, discuss specifics, or take any position "
            "on this topic. A separate, pre-approved note about this topic will be "
            "appended to your reply automatically after you write it. That note already "
            "tells the tenant the topic has been passed to the landlord, who will follow "
            "up with them directly -- so your own sentences must NOT say you've passed "
            "anything along, must NOT promise follow-up, and must NOT sign off (no 'talk "
            "soon', no closing line -- more text follows yours). Write ONLY a short, warm "
            "acknowledgment that you received their message, one or two short sentences, "
            "and do NOT write, quote, paraphrase, or summarize any standard policy "
            "language yourself."
        )
        if RefusalFlag.access_codes in refusal_flags:
            lines.append(_ACCESS_ALTERNATIVE_GUIDANCE)

    available_chars = _available_ack_chars(refusal_flags)
    lines.append(
        f"\nKeep your reply to at most {available_chars} characters (SMS -- every "
        "character counts)."
    )

    lines.append(f"\n{_PLAIN_LANGUAGE_REMINDER}")
    return "\n".join(lines)


async def draft_response(state: AgentState) -> dict[str, Any]:
    """Draft an SMS reply in the landlord's voice. Returns a partial state
    update. Always inserts a draft when a case is known — the fallback path
    (see module docstring) guarantees the queue never goes empty because a
    guard tripped twice."""
    message_id = state["message_id"]
    case_context = state.get("case_context") or CaseContext()
    reasoning_log = list(state.get("reasoning_log") or [])
    severity_result = state.get("severity")

    if case_context.case_id is None:
        reasoning_log.append(
            "There's no conversation to draft a reply on yet, so I skipped drafting."
        )
        log.error("draft_response_missing_case_id", message_id=str(message_id))
        return {"reasoning_log": reasoning_log}

    if severity_result is None:
        reasoning_log.append(
            "I can't draft a reply yet — severity hasn't been figured out for this message."
        )
        log.error(
            "draft_response_missing_severity",
            message_id=str(message_id),
            case_id=str(case_context.case_id),
        )
        return {"reasoning_log": reasoning_log}

    async with asynccontextmanager(get_admin_session)() as session:
        body: str = (
            (await session.execute(_SELECT_MESSAGE_SQL, {"message_id": str(message_id)}))
            .mappings()
            .one()
        )["body"]

        tenant_name: str | None = None
        if case_context.tenant_id is not None:
            tenant_row = (
                (
                    await session.execute(
                        _SELECT_TENANT_NAME_SQL, {"tenant_id": str(case_context.tenant_id)}
                    )
                )
                .mappings()
                .one_or_none()
            )
            tenant_name = tenant_row["name"] if tenant_row is not None else None

    system_prompt = build_draft_system_prompt(case_context.voice_profile)
    base_user_content = _build_user_content(
        body=body,
        tenant_name=tenant_name,
        house_rules=case_context.house_rules,
        severity_result=severity_result,
        refusal_flags=severity_result.refusal_flags,
    )

    # Documented exception (see module docstring "Length discipline"): a
    # refusal-flagged message never has its length enforced -- the
    # mandated deferral legitimately makes it longer, and correctness
    # outweighs segment-length there.
    enforce_length_budget = not severity_result.refusal_flags
    available_chars = _available_ack_chars(severity_result.refusal_flags)

    ack_body: str | None = None
    last_candidate_body: str | None = None
    last_violations: list[str] = []
    call_errors: list[str] = []
    user_content = base_user_content
    deadline = anthropic_mod.new_deadline()
    total_tokens_in = 0
    total_tokens_out = 0
    last_model: str | None = None

    for attempt in range(2):
        timeout = anthropic_mod.attempt_timeout(deadline, is_retry=attempt == 1)
        if timeout is None:
            log.error("draft_response_retry_skipped_budget_exhausted", message_id=str(message_id))
            break
        try:
            call_result = await anthropic_mod.call_tool_forced(
                system=system_prompt,
                user_content=user_content,
                tool=DRAFT_MESSAGE_TOOL,
                tool_name="draft_message",
                timeout_seconds=timeout,
            )
            candidate = DraftResult.model_validate(call_result.tool_input)
        except (anthropic_mod.AnthropicCallError, ValidationError) as exc:
            call_errors.append(type(exc).__name__)
            log.error(
                "draft_response_attempt_failed",
                message_id=str(message_id),
                attempt=attempt,
                exc_type=type(exc).__name__,
            )
            continue

        total_tokens_in += call_result.tokens_in
        total_tokens_out += call_result.tokens_out
        last_model = call_result.model

        violations = _check_hard_guards(body=candidate.body)
        too_long = enforce_length_budget and len(candidate.body) > _LENGTH_BUDGET_CHARS
        last_candidate_body = candidate.body
        last_violations = violations

        if not violations and not too_long:
            ack_body = candidate.body
            break

        if violations:
            log.warning(
                "draft_response_guard_violation",
                message_id=str(message_id),
                attempt=attempt,
                violations=violations,
            )
            user_content = base_user_content + _violation_retry_note(violations)
        else:
            log.warning(
                "draft_response_length_violation",
                message_id=str(message_id),
                attempt=attempt,
                length=len(candidate.body),
            )
            user_content = base_user_content + _length_retry_note(available_chars)

    guard_failed = False
    length_over_budget = False
    if ack_body is None:
        if last_candidate_body is not None and not last_violations:
            # Guards were clean -- the ONLY remaining problem was length.
            # TRUNCATION IS FORBIDDEN: keep the long draft, flag it instead.
            ack_body = last_candidate_body
            length_over_budget = True
            reasoning_log.append(
                "This reply came out longer than usual — you can shorten it before it goes out."
            )
            log.warning(
                "draft_response_length_over_budget_kept",
                message_id=str(message_id),
                case_id=str(case_context.case_id),
                length=len(ack_body),
            )
        else:
            guard_failed = True
            ack_body = _GENERIC_SAFE_FALLBACK
            reasoning_log.append(
                "I wasn't confident this draft was safe to send as-is, so I used a safer "
                "standard reply instead — worth a look before it goes out."
            )
            log.error(
                "draft_response_guard_failed_after_retry",
                message_id=str(message_id),
                case_id=str(case_context.case_id),
                violations=last_violations,
                call_errors=call_errors,
            )
    else:
        reasoning_log.append("I've drafted a reply for you to review.")

    final_body = _append_deferrals(ack_body, severity_result.refusal_flags)
    draft_result = DraftResult(
        body=final_body, refusal_templates_used=list(severity_result.refusal_flags)
    )
    cost_cents = anthropic_mod.estimate_cost_cents(
        tokens_in=total_tokens_in, tokens_out=total_tokens_out
    )

    async with asynccontextmanager(get_admin_session)() as session:
        pending_row = (
            (
                await session.execute(
                    _SELECT_PENDING_DRAFT_SQL, {"case_id": str(case_context.case_id)}
                )
            )
            .mappings()
            .one_or_none()
        )

        if pending_row is not None:
            stale_draft_id: UUID = pending_row["id"]
            await session.execute(_UPDATE_DRAFT_STALE_SQL, {"draft_id": str(stale_draft_id)})
            await session.execute(
                _INSERT_DRAFT_STALE_AUDIT_SQL,
                {
                    "landlord_id": str(case_context.landlord_id),
                    "case_id": str(case_context.case_id),
                    "payload": json.dumps({"draft_id": str(stale_draft_id)}),
                },
            )
            reasoning_log.append(
                "A new message came in, so I marked the earlier draft as out of date and "
                "wrote a fresh one."
            )

        new_draft_row = (
            (
                await session.execute(
                    _INSERT_DRAFT_SQL,
                    {
                        "landlord_id": str(case_context.landlord_id),
                        "case_id": str(case_context.case_id),
                        "body": draft_result.body,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
            )
            .mappings()
            .one()
        )
        new_draft_id = new_draft_row["id"]

        await session.execute(
            _INSERT_DRAFTED_AUDIT_SQL,
            {
                "landlord_id": str(case_context.landlord_id),
                "case_id": str(case_context.case_id),
                "payload": json.dumps(
                    {
                        "draft_id": str(new_draft_id),
                        "refusal_templates_used": [
                            flag.value for flag in draft_result.refusal_templates_used
                        ],
                        "guard_failed": guard_failed,
                        "model": last_model,
                        "tokens_in": total_tokens_in,
                        "tokens_out": total_tokens_out,
                        "cost_cents": cost_cents,
                    }
                ),
            },
        )

    return {
        "draft": draft_result,
        "draft_guard_failed": guard_failed,
        "length_over_budget": length_over_budget,
        "reasoning_log": reasoning_log,
    }


__all__: list[str] = ["draft_response"]
