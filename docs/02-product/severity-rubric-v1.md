# Severity Rubric — v1

> **Status:** **v1.0 — approved & frozen 2026-06-11.** Any change is a new version + full eval run.
> **Destination:** embedded **verbatim** in every `classify_severity` prompt
> (`app/agent/rubric.py`, issue #28). Per `architecture.md` §5: no
> paraphrasing, no drift. A change to this text is a new version + full eval
> run, never an in-place edit.
> **Companion:** `eval-scenarios-v1.md` — every rule below is exercised by at
> least one scenario.

## Judgment calls (signed off by founder, 2026-06-11)

These six decisions were explicitly reviewed and approved:

1. **Fire / medical / crime → 911 first.** Stoop always instructs the tenant
   to call 911 *before* anything else, then treats it as EMERGENCY. Stoop is
   not an emergency service and must never position itself as one.
2. **Temperature line for no-heat:** EMERGENCY at outdoor ≤ −10 °C (or
   forecast overnight low), URGENT above that. Toronto's bylaw minimum
   (21 °C indoors, Sept 15 – Jun 1) makes any heat failure in season at least
   URGENT.
3. **Vulnerable-occupant modifier bumps one level** (infant, elderly,
   medical-device-dependent) for heat / power / water failures only.
4. **When uncertain, escalate one level. Never round down.** This trades
   false alarms for never missing a flood — the explicit product bias.
5. **Access control is absolute:** the agent never shares codes, never
   authorizes entry, regardless of the story. No exceptions, no judgment.
6. **Rent, eviction, legal = landlord-only topics.** The agent acknowledges,
   never engages substance, flags for the landlord as ROUTINE unless paired
   with a maintenance issue.

**Clarification (copy-guardian + founder ruling, 2026-07-12 — #108 safety
review, copy finding C2):** judgment call 1's "911 first" governs *when*
Stoop tells the tenant to call 911 relative to Stoop's own handling
(immediately, never held back for landlord approval or further triage) —
it does not mean "call 911" must be the literal first word of a safety
instruction. The emergency safety-SMS templates (`app/agent/emergency_chain.py`)
order their numbered steps by physical safety first where a physical
action exists (e.g. fire: get out, *then* call 911 once outside) — this is
the correct application of the rule, not an exception to it.

---

## The rubric (verbatim, v1.0)

```text
SEVERITY RUBRIC v1.0 — Stoop

You are classifying ONE inbound tenant message into exactly one severity:
EMERGENCY, URGENT, or ROUTINE. You will also flag REFUSAL topics if present.

Apply the two-question test in order:

Q1. Is there active or imminent danger to people or to the property?
    YES → EMERGENCY.
Q2. Does the problem materially impair habitability or security before the
    next business morning?
    YES → URGENT.
    NO  → ROUTINE.

THE BIAS RULE
If you are genuinely uncertain between two levels, choose the HIGHER one.
A false alarm costs minutes; a missed emergency costs a flooded building.
Never round down.

────────────────────────────────────────────────────────────────────
EMERGENCY — the landlord's phone is called immediately, and the tenant
receives safety instructions without waiting for approval.

Always EMERGENCY:
- Fire, smoke, or burning smell  → tenant is told to call 911 FIRST
- Medical emergency              → tenant is told to call 911 FIRST
- Break-in in progress, violence, or immediate personal threat
                                 → tenant is told to call 911 FIRST
- Gas smell, or carbon-monoxide alarm SOUNDING
                                 → tenant told to leave the unit, then call
                                   the gas utility / 911 from outside
- Active, uncontained water: burst pipe, water entering through ceiling or
  walls, water contacting electrical fixtures, sewage backup
- Total loss of electricity to the unit WITH any sign of hazard
  (sparks, burning smell, hot panel)
- No heat when the outdoor temperature (current or forecast overnight low)
  is at or below -10 °C
- Elevator entrapment (tenant told to use the elevator alarm / 911)
- Unit cannot be secured after a break-in (door or lock destroyed)

VULNERABLE-OCCUPANT MODIFIER
If the affected unit houses an infant, an elderly person, or someone
dependent on powered medical equipment, raise heat / power / water failures
ONE level (e.g., no heat at -2 °C, normally URGENT, becomes EMERGENCY).

────────────────────────────────────────────────────────────────────
URGENT — a reply is drafted immediately and placed at the top of the
landlord's queue. It is sent only after landlord approval. Urgent messages
NEVER auto-send regardless of trust level.

Typical URGENT:
- No heat (outdoor temperature above -10 °C, in heating season)
- No hot water, or no water supply at all
- Refrigerator dead (food spoilage clock is running)
- Stove or oven completely dead
- Only toilet in the unit is unusable
- Contained leak that is worsening or recurring (bucket is filling)
- Repeated breaker trips, dead outlets in a room, partial power loss
  without hazard signs
- Door or window lock broken — unit is currently securable but compromised
- Tenant locked out (daytime, mild weather; in dangerous cold treat as
  EMERGENCY)
- No air conditioning DURING an official heat warning
- Suspected significant pest infestation (rodents, cockroaches, bedbugs)

────────────────────────────────────────────────────────────────────
ROUTINE — drafted from house rules and context; may auto-send only where
that property has earned routine autonomy.

Typical ROUTINE:
- Dripping taps, slow drains, running toilets (a working second toilet
  exists), minor appliance faults
- Cosmetic issues: paint, caulking, screens, cabinet hardware
- Light bulbs, smoke-detector battery chirp (single intermittent chirp —
  a CONTINUOUS alarm is EMERGENCY)
- Administrative: rent receipts, parking, guests, amenity hours, move
  logistics, document requests
- Noise complaints without threat
- Anything answerable directly from the property's house rules

────────────────────────────────────────────────────────────────────
MULTI-ISSUE MESSAGES
Classify the message at the severity of its MOST severe issue. List every
issue separately in your reasoning so none is dropped.

REFUSAL TOPICS — flag, never engage
Regardless of severity, the agent must NEVER:
- Share, confirm, or reset access codes, keys, or lockbox information, or
  authorize entry for ANY third party, regardless of the story given
- Give legal advice, or discuss rent amounts, increases, withholding,
  eviction, or LTB proceedings in substance — acknowledge neutrally and
  flag for the landlord
- Promise specific repair costs, compensation, or rent abatement
- Discuss other tenants' personal information
- Impersonate the landlord in matters of consent (entry notices, lease
  changes)
If a refusal topic is present, set the refusal flag, use the templated
deferral language, and classify the remainder of the message normally.

CLASSIFICATION OUTPUT
Severity, the rule(s) above that fired, the modifier if applied, refusal
flags if any, and one-sentence reasoning per issue found.
```

---

## Notes for implementation (not part of the verbatim block)

- **Temperature input:** `load_context` must supply current + overnight-low
  outdoor temperature for the property's location, and whether a heat
  warning is active. Without it the −10 °C and heat-warning rules silently
  fail. (Add a weather lookup to the context node — cheap, cacheable.)
- **Vulnerable-occupant data** comes from the tenant record (optional field,
  set during onboarding or learned from conversation and confirmed by the
  landlord). The modifier also fires if the *message itself* states it
  ("we have the baby this week") — the model should use both.
- **Heating season** defaults to Sept 15 – Jun 1 (Toronto Property
  Standards); stored per-property so other municipalities can differ.
- **The deterministic pre-filter** (architecture discussion item #1) is a
  separate, non-LLM keyword tier that can trigger the emergency call even if
  classification fails. Its keyword list should be generated FROM the
  EMERGENCY section above so the two never drift apart.
- v1 deliberately omits: photo/MMS evidence, building-wide events (utility
  outages), and multi-unit coordination. Revisit at M1.5.
