---
title: "roadmap: Stoop — release train"
labels: ["roadmap"]
milestone: ""
---

# Stoop · Roadmap (v3 — release train)

> **Rewritten 2026-06-11 (second revision, same day).** Supersedes the
> 3-milestone roadmap after two founder decisions: (1) every planned
> feature is **committed and ordered**, not gated behind "deferred" labels;
> (2) the founder is **full-time with Claude Code**, compressing build
> estimates ~3–4×. Architecture unchanged (`../03-engineering/architecture.md`).
> GitHub milestones renamed to match: Train 1 / Train 2 / Train 3.

**The principle:** ship continuously; first real tenant text at ~week 2–3,
everything after built on evidence. Code compresses with tooling —
**evidence doesn't**: A2P/CASL registration, pilot recruiting, and
trust-data accumulation move at the world's speed, so they all start on
day one and run in parallel with the build.

**What can't ship early no matter the velocity (physics, not priorities):**
trust-ladder *activation* (consumes weeks of real approvals), trust LV3
(more of the same), the marketplace (needs measured no-vendor demand +
supply seeded from real landlords), and Stoop Desk's integration choice
(made by actual PM waitlist customers). All are committed; each activates
when its fuel exists.

---

## Train 1 · v1.0 – v1.1 — Core loop + photos (~weeks 1–4)

**v1.0 (~wk 2–3):** the full loop, live in production for the founder's
own properties.
- Foundation: FastAPI, Supabase Auth, Fly.io, CI, Sentry, logging
  *(issues #1–#16)*
- Schema incl. cases/messages/audit_log/**vendors** *(#17–#19, #21, #24)*
- Agent: identify → context (weather) → intent → severity (rubric v1.0)
  → draft → interrupt → send; emergency branch *(#26–#37, #110)*
- Emergency system: Tier-0 pre-filter, escalation chain, degraded mode
  *(#107–#109)*
- **Vendor coordination, approval-first** *(#115)*
- Approval queue dashboard (5 s undo, reasoning trace), Twilio loop,
  e2e test, cost metering, 10 evals in CI *(#40, #43–#45, #50, #61, #73,
  #111)*
- **Day-one parallel starts:** A2P/CASL filing, design-partner outreach
  (`design-partners.md`), trust-metrics recording (fuel for v1.3)

**v1.1 (~wk 4):**
- **Photos/MMS** *(#46, reopened)*: Twilio media → Supabase Storage →
  image into classification (Claude is vision-capable)
- Basic hardening: backups verified, rate limits on public endpoints

**Gate:** real tenant texts (incl. a photo) handled end-to-end; 10 evals
green; zero missed emergencies.

---

## Train 2 · v1.2 – v1.4 — Open doors, revenue, mobile (~weeks 5–10)

**v1.2 — strangers can join:**
- Self-serve onboarding (voice profile, house rules, disclosure) *(#113)*
- RLS on every table + cross-tenant isolation suite *(#20, #22, #23, #64)*
- Full API surface *(#53–#57)*; `auth.users` trigger *(#15)*
- Brownstone dashboard port + landing live *(#112)*
- Pilots onboarding in cohorts *(#98–#100)*

**v1.3 — revenue + agent maturity:**
- Stripe: Emergency Line free / $10 Full Plan / $5 early-access
  grandfathered *(#52, #58, #59)*
- **Trust ladder activates** — the approval history recorded since v1.0
  now exists; routine auto-send unlocks per property *(#60)*
- Agent upgrades: clarification, vendor_match, output filter, prompt
  caching, 50-scenario evals *(#66–#70)*
- Pilot feedback loop *(#101–#103)*

**v1.4 — reach:**
- Mobile shell + push (approve from a notification in <10 s) *(#116)*
- Multi-step agent jobs v1: quote → schedule → verify → close *(#117)*
- Full hardening pass: runbook, alerts, unit-economics queries *(#71, #72)*

**Gate:** a stranger onboards unaided AND pays; isolation suite green;
trust auto-send live on ≥1 property.

---

## Train 3 · v2.0+ — Stoop Desk, LV3, marketplace (condition-triggered)

- **v2.0 Stoop Desk** *(#118)*: org multi-tenancy, teams, on-call
  rotations, first PM integration (chosen by waitlist customers),
  resident-benefit billing, SOC 2 kickoff.
  *Trigger: ≥25 qualified PM waitlist signups (already capturing via
  `/early-access` checkbox).*
- **Trust LV3**: hands-off vendor scheduling for proven properties.
  *Trigger: trust data, not code.*
- **v3.0 Marketplace** *(#119)*: booking-fee revenue, cold-started from
  other landlords' proven vendors nearby.
  *Trigger: ≥20% of cases measurably end "no vendor for this trade."*
- Provincial compliance packs, US decision, insurance partnerships per
  `three-year-plan.md`.

---

## Standing rules

- The emergency line is never paywalled.
- Prompt/rubric changes = new version + full eval run (CI-enforced).
- Scaling work on triggers (`../03-engineering/architecture.md` §11), not on faith.
- Every production misclassification becomes an eval case the same week.

## Issue bookkeeping

GitHub milestones = trains. Current distribution: Train 1 ≈ 46 issues,
Train 2 ≈ 31, Train 3 ≈ 4 umbrellas (children created when triggers fire).
Phase-1 specs in `phase-1/issues/` remain the detailed references.
