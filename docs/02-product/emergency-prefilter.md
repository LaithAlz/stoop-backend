# Emergency Pre-filter & Degraded Mode — the LLM-independent safety path

> **Status:** Draft for founder review, 2026-06-11.
> **Principle (from architecture review):** Stoop is a safety system with
> an AI in it, not an AI product. The emergency path must work when the
> LLM is slow, down, or wrong. This doc specifies the deterministic tier,
> the degraded mode, and the don't-answer escalation chain.

## Tier 0: deterministic keyword filter

Runs **synchronously in the Twilio webhook handler**, on the raw message,
before the agent is even invoked. Pure regex over a normalized string
(lowercased, punctuation stripped). No network calls. Sub-millisecond.

**HARD triggers** — any match fires the emergency protocol immediately,
without waiting for classification:

| Category | Patterns (illustrative, not exhaustive) |
|---|---|
| Fire | `fire`, `smoke` + (`smell\|filling\|everywhere`), `burning smell` |
| Gas/CO | `gas` + (`smell\|leak\|smells like`), `carbon monoxide`, `co alarm\|detector going off` |
| Water | `flood(ing)?`, `burst pipe`, `water` + (`pouring\|gushing\|coming through\|through the ceiling`), `sewage` |
| Security | `break(ing)? in`, `broke in`, `intruder`, `someone is trying to get in` |
| Person | `911`, `ambulance`, `can't breathe`, `unconscious` |

**Guarded negatives** — narrow exclusions for known false positives,
checked before firing: `smoke detector` + (`battery\|chirp\|beep`) alone,
`fire drill`, `fire alarm test(ing)?`. The list of exclusions is small on
purpose: per the rubric's bias rule, a false emergency call costs minutes;
a missed one costs the building. When a guard and a trigger both match,
**the trigger wins**.

**SOFT annotations** — `no heat`, `freezing`, `sparks`, `leak`, `locked
out`: never fire the protocol alone, but are attached to the message
record and (a) lower the degraded-mode bar (below) and (b) are surfaced
to the classifier as hints.

**Maintenance rule:** the HARD list is *generated from* the rubric's
EMERGENCY section and versioned with it (`prefilter v1.0 ⇄ rubric v1.0`).
Eval scenarios E1 and E2 must trip Tier 0 (add `prefilter_must_fire: true`
to both), and a negative suite asserts the guards (R-class "detector
chirping" must NOT fire). The two layers can never drift apart silently.

**PrefilterResult shape** (`app/agent/schemas.py`, the canonical type
`check()` returns and that is snapshotted into `messages.prefilter` jsonb):

| Field | Type | Meaning |
|---|---|---|
| `hard_hit` | bool | True if any HARD trigger category matched |
| `categories` | list[str] | HARD categories that fired: `fire`, `gas_co`, `water`, `security`, `person` |
| `soft_annotations` | list[str] | SOFT matches: `no_heat`, `freezing`, `sparks`, `leak`, `locked_out` |
| `guards` | list[str] | guard patterns that matched but were overridden by a trigger (kept for review) |

## How Tier 0 and the classifier compose

```
inbound SMS ──► persist message ──► Tier 0 regex ──┬─ HARD hit ──► EMERGENCY
                                                   │               PROTOCOL (now)
                                                   │               + agent still runs
                                                   │                 (adds context,
                                                   │                  drafts follow-ups)
                                                   └─ no hit ───► agent classifies
                                                                  normally (rubric)
```

- Tier 0 firing is **idempotent with** the agent's own emergency
  classification: the protocol records `triggered_by: prefilter|agent`
  and fires **once per case** (dedupe on case id). The agent can escalate
  a Tier-0 miss; it can never *de-escalate* a Tier-0 fire — if the LLM
  disagrees, the call already happened, and that's the bias rule working
  as designed. The disagreement is logged and reviewed (it's either a
  guard candidate or an eval case).

## Degraded mode (LLM unavailable or too slow)

Classification budget: **20 seconds** end-to-end (generous; typical is
2–5 s). On timeout, API error, or hard failure after one retry:

| Condition | Behavior |
|---|---|
| Tier 0 HARD hit | already handled — protocol fired without the LLM |
| SOFT annotation present | **escalate blind**: landlord gets an URGENT-style notification ("Couldn't auto-classify — tenant message needs your eyes: ⟨raw text⟩"), tenant gets the holding ack |
| no keywords at all | tenant gets the holding ack; message queued for re-classification (retry at 1, 5, 15 min); if still failing at 15 min, landlord gets the needs-your-eyes notification anyway |

**Holding ack (template, no LLM):** "Got your message — it's been passed
to ⟨landlord first name⟩ and you'll hear back soon. If this is a
life-threatening emergency, call 911."

The invariant: **no tenant message ever sits unacknowledged and invisible
because an API was down.** Worst case, the landlord reads raw texts for an
hour — which is exactly what they did before Stoop existed.

## The escalation chain (landlord doesn't answer)

v1 contacts: the landlord + one optional **backup contact** per property
(partner, super, trusted neighbor — configured at onboarding, strongly
encouraged, not required).

```
T+0     voice call to landlord (Twilio, with spoken summary + "press 1
        to acknowledge")
T+0     safety SMS to tenant already sent (category template)
T+2m    if unacknowledged: SMS to landlord — "🚨 EMERGENCY at ⟨property⟩:
        ⟨summary⟩. Call ⟨tenant⟩ or press link to acknowledge."
T+5m    second voice call to landlord
T+10m   backup contact: voice call + SMS (if configured)
T+15m   third call to landlord; tenant receives honest status: "Still
        reaching ⟨name⟩ — if the situation is getting dangerous, call 911."
T+20m+  repeat landlord+backup cycle every 15 min until acknowledged;
        every attempt in audit_log
```

**Acknowledgment** = pressing 1 on the call, tapping the SMS link, or
opening the case in the dashboard. It stops the chain and stamps
`acknowledged_at` (the metric "median time to acknowledgment" comes free).

What v1 deliberately does NOT do: call plumbers/trades autonomously
(trust LV3+), call 911 on anyone's behalf (the tenant is instructed to —
Stoop is not an emergency service, per rubric sign-off #1), or page a
paid human escalation service (revisit if pilots show landlords sleeping
through three calls).

## Implementation notes

- Tier 0 lives in `app/agent/prefilter.py` as pure functions
  (`check(text) -> PrefilterResult`) — trivially unit-testable, no I/O.
- The escalation chain is a state machine on the `notifications` table
  driven by scheduled checks — **this is the one place v1 genuinely wants
  a durable timer.** Implementation: a `fly machines` cron / APScheduler
  loop checking unacknowledged emergencies every 60 s is sufficient at
  pilot scale; this is also the first candidate to move onto a real queue
  when ADR-2's trigger fires.
- Twilio voice: a single TwiML app that speaks the summary and gathers
  the keypress. One afternoon of work; spec it as a new M1 issue alongside
  #40.

## Open judgment calls for founder

1. **20 s classification budget** before degraded mode — confirm.
2. **Escalation timings** (2/5/10/15 min) and the T+15m honest-status
   message to the tenant — confirm wording comfort.
3. **Backup contact** strongly-encouraged-but-optional at onboarding —
   or make it required for the emergency promise to feel honest?
4. Agent can never de-escalate a Tier-0 fire — accepts occasional
   "called you for a fire-drill mention" false positives. Confirm.
