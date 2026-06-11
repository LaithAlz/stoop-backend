# Stoop — Three-Year Plan (mid-2026 → mid-2029)

> **Status:** Draft for founder review, 2026-06-11.
> **What this is:** the business and technology plan on one page-set, with
> explicit decision gates. Children of this doc: `architecture.md` (tech),
> `business-model.md` (pricing/GTM), `stoop-all-epics.md` (build roadmap),
> `design-partners.md` (pilot recruiting).
> **How to read it:** each horizon has a theme, business goals, tech
> evolution, and a gate. Gates are conditions, not dates — dates are
> planning fiction; conditions are real.

---

## The throughline

Three acts, each funding the next:

1. **Act 1 — The inbox** (H0–H1): own the tenant-message relationship for
   self-managing Ontario landlords. Revenue: subscriptions ($10/mo, $5
   early-access). Moat being built: eval corpus + trust data + voice
   profiles.
2. **Act 2 — The desk** (H2): sell after-hours coverage to property
   managers ($1.50/door + resident-benefit margin) and expand
   Canada-wide. Moat being built: PM integrations + per-province
   compliance packs.
3. **Act 3 — The network** (H3): when enough landlords have no plumber,
   supply one — vendor marketplace with booking fees; insurance
   partnerships on the audit-trail data. Moat being built: two-sided
   network + claims-grade documentation.

Each act's product is the previous act's distribution. The inbox earns the
trust that sells the desk; the desk aggregates the demand that powers the
network.

---

## Horizon 0 · "It works and strangers pay" — now → ~Q1 2027

**Theme:** ship the three milestones, run the pilots, charge money.

### Business
- Execute `design-partners.md`: outreach starts week 1 of the build;
  10 pilots live by M2; pricing study (#102) in pilot retros.
- Waitlist live at `/early-access` (done) — every channel points at it.
- Start the Ontario SEO machine: 2 posts/month + the "is this an
  emergency?" checker (the rubric as a free tool). Compounds for years;
  starts now.
- Targets at horizon end: **25–50 paying landlords, ~$300–500 MRR,
  ≥ 5 written testimonials, churn < 3%/mo.** (Small numbers on purpose —
  this horizon buys *proof*, not revenue.)

### Tech (mostly already specced)
- M1 walking skeleton → M2 multi-landlord → M3 money, per
  `stoop-all-epics.md`. Includes vendor coordination v1 (#115),
  emergency pre-filter + escalation chain (#107–109), eval suite,
  cost metering, audit log.
- MMS/photos pulled in at M1.5 (the deferral most likely to be wrong).
- Founder reviews every classification in LangSmith for each pilot's
  first two weeks. Every miss → eval case. Corpus target: **50 scenarios.**

### Gate to H1
> 10+ strangers pay full price unprompted, churn is boredom-free
> (< 3%/mo), and zero missed emergencies across the pilot fleet.

**Kill/pivot signal:** pilots use it but won't pay even $5, or tenants
refuse to text the new number at meaningful rates. Then the wedge is wrong
— revisit (likely pivot: sell the audit trail / documentation product to
paralegals instead of comms to landlords).

---

## Horizon 1 · "Ontario beachhead" — ~2027

**Theme:** repeatable acquisition; the product runs without the founder
watching every message.

### Business
- Channels proven one at a time, measured by the waitlist `source`
  column: SEO content → referral mechanic (free month per referred
  landlord) → Ontario REI partnerships/podcasts → paid only if organic
  CAC is understood.
- Standard price $10 holds; early-access cohort closes when support load
  says so (not at a public number). First price test for new customers
  per #102 data.
- **Targets: 300–500 paying landlords (~600–1,000 doors), $4–8k MRR,
  NRR > 100% via door growth, CAC < 3 months of revenue.**
- **First hire trigger** (contractor before employee): founder spends
  > 50% of week on support/onboarding instead of product. Likely a
  part-time landlord-success person from the pilot cohort.

### Tech
- **Mobile**: the dashboard is already mobile-first; ship wrappers (Expo
  shell over the web app) only when paying landlords ask — push
  notifications are the real feature, not the app.
- **Trust ladder LV3 live**: vendor auto-coordination for proven
  properties (approval-first vendor texting shipped in v1; LV3 removes
  the tap for routine vendor scheduling).
- Scaling triggers fire as they fire (architecture §11): durable queue on
  webhook losses; replica on p95 > 300 ms. Expect the queue around
  ~100 landlords.
- Eval corpus **150–200 scenarios**; rubric v2 with seasonal/regional
  variants; prompt-regression CI is the deploy gate.
- Model strategy: re-benchmark the classify/draft pair against current
  Claude models twice a year on the eval suite; cost per message should
  fall every cycle — pocket the margin.

### Gate to H2
> A repeatable channel produces customers at known CAC without founder
> heroics, AND the PM waitlist has ≥ 25 qualified PM signups (proof of
> pull, not push).

---

## Horizon 2 · "The desk + the rest of Canada" — ~2028

**Theme:** two segments, one platform. This is the first horizon where
the company stops looking like a side project.

### Business
- **Stoop Desk launches** (PM product): $1.50–2/door platform fee +
  resident-benefit package (the Latchel model — PM keeps the margin,
  Stoop becomes a profit center on their P&L). Distribution: Buildium /
  AppFolio / Rent Manager app marketplaces + the warmed waitlist.
- **Canada expansion** beyond Ontario: BC, Alberta, Quebec (Quebec last —
  language + civil-law differences are real work). Each province ships
  as a *compliance pack*: bylaw thresholds, notice rules, rubric
  variants, eval scenarios. The packs are productized regulatory depth —
  competitors must rebuild them province by province.
- Pricing evolves: standard tier likely $12–15 for new cohorts if H1
  data supports it; early customers stay grandfathered (the promise is
  the brand).
- **Targets: $25–50k MRR blended** (≈ 1,500 landlords + 5–10k PM doors),
  gross margin back above 80% as scale amortizes fixed costs.
- **Team at horizon end: ~3–4 people.** Founder + landlord-success +
  first engineer (trigger: roadmap consistently blocked on founder
  bandwidth, not taste) + part-time content.
- **Fundraise decision point** (deliberately here, not earlier): by now
  the data says whether this is a $3–5M ARR lifestyle business (don't
  raise; profitable and calm) or the marketplace act looks real (raise a
  seed to accelerate Act 3). Both outcomes are wins; decide on NRR + PM
  pull + marketplace signal, not on ambition.

### Tech
- **Integrations layer**: work orders out to PM software (their system of
  record, our system of intelligence). This is a real engineering
  surface — budget it like a product, not a feature.
- **Teams/roles/SLAs + SOC 2** (the PM segment's entry fee; trigger
  already defined in architecture §11).
- Org-level multi-tenancy: RLS model extended from landlord-scoped to
  org-scoped (PM company → coordinators → portfolios). Schema groundwork
  exists (uuid keys, audit actor field) — the migration is real but not
  a rewrite.
- On-call/escalation chains become multi-assignee (PM rotations) — the
  notifications state machine from #108 generalizes.
- Eval corpus **400+**; per-province scenario suites; classification
  quality is now a published number (status page for accuracy — sales
  asset and discipline).

### Gate to H3
> PM cohort retention > 90% annual, resident-benefit attach rate proves
> tenants will pay for response SLAs, AND ≥ 20% of maintenance cases
> end with "I don't have a guy" (measured, not guessed).

---

## Horizon 3 · "The network" — ~2029

**Theme:** monetize the demand the inbox and desk aggregate.

### Business
- **Vendor marketplace**: vetted trades for landlords without one;
  booking fee or take rate per job. Cold-start solved property-by-
  property: the system already knows every landlord's own vendors (since
  v1) — the marketplace begins as overflow routing to *other landlords'
  proven vendors* in the same neighbourhood, not as a cold directory.
- **Insurance partnerships**: claims-grade maintenance documentation
  (the append-only audit trail, since v1) as underwriting signal —
  discounts/referrals from landlord insurers. Slow to close; start
  conversations in H2.
- **US entry decision** — only after marketplace economics are proven in
  Canada. US = state-by-state compliance packs (the H2 machinery) +
  A2P from day one + competition that actually exists (Latchel, Property
  Meld). Enter with the network, not the inbox: the inbox is cloneable,
  the network is not.
- **Targets: $100k+ MRR blended, marketplace GMV being measured in the
  millions, team ~6–10.**

### Tech
- Marketplace services (vendor onboarding, dispatch, payments via Stripe
  Connect, ratings) — the first genuinely new system since v1; likely
  the moment the monolith grows a second service, and the first time
  multi-region matters (US data residency).
- Agent evolution: multi-step jobs (quote → schedule → verify → close
  the loop with the tenant) — LangGraph durable workflows earn their
  keep here; the v1 learning investment pays out.
- ML beyond prompts: severity classification potentially distilled to a
  fine-tuned small model (cost + latency) with the frontier model as
  escalation tier — the 400+ eval corpus makes this safe to attempt.

---

## Standing metrics (tracked from M1, reviewed monthly)

| Metric | Why it's the dashboard |
|---|---|
| Quiet nights / true emergencies caught | The product promise, measured |
| Missed-emergency count | The only metric where the target is **zero, forever** |
| Approval rate without edits | Trust ladder fuel + drafting quality |
| Median time-to-acknowledgment (emergencies) | The escalation chain working |
| Cost per door vs revenue per door | Unit economics, from #111 metering |
| Doors per account over time | Expansion revenue = NRR engine |
| Waitlist source → paid conversion | Which channel actually works |
| Eval pass rate + corpus size | The moat, quantified |

## Standing risks (reviewed quarterly)

1. **Missed emergency with harm** — existential at any scale. Controls:
   pre-filter, bias rule, escalation chain, zero-target metric, insurance.
2. **Carrier/SMS policy shifts** (A2P tightening) — stay registered,
   keep volumes clean; long-term hedge is the mobile app channel.
3. **Platform pricing** (Anthropic/Twilio) — metering since message one;
   model re-benchmarks twice yearly; margin trends are watched, not felt.
4. **PM-platform bundling** (the Mezo path — triage given away free
   inside Buildium et al.) — the defense is owning the landlord
   relationship and the compliance packs; never become a feature vendor.
5. **Founder burnout** — the hire triggers above are honesty mechanisms;
   grandfathered pricing and small-numbers targets keep promises keepable.

## What this plan refuses to do

- Set revenue *dates* (gates are conditions; the dates above are ~fuzzy).
- Raise money before H2's decision point produces evidence either way.
- Enter the US with the cloneable product instead of the network.
- Compete for enterprise multifamily (EliseAI owns it; let it validate
  the category).
- Paywall the emergency line. Ever. It's the brand, the moat's front
  door, and the reason tenants and landlords both root for us.
