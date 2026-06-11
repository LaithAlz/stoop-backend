# Business Model — segments, pricing, and the traction playbook

> **Status:** Draft for founder review, 2026-06-11. Based on competitive
> research (sources at bottom) + the unit-economics work in
> `architecture.md` §9 / issue #111.

## 1. What the competition teaches

| Company | Segment | Model | Traction mechanism | Lesson for Stoop |
|---|---|---|---|---|
| **TurboTenant** | 1–10 door landlords | Freemium; ~$149/yr premium; tenant-paid screening fees | Free core tools + massive SEO/education machine; 1M+ landlords | Freemium + content WORKS in this market; monetization can sit on premium convenience |
| **Avail** (→ Realtor.com) | small landlords | Free unlimited units; $9/unit/mo plus tier | Freemium + marketplace distribution; acquired for distribution | Per-unit premium at single-digit $ is accepted |
| **RentRedi** | small landlords | Paid-only, ~$30/mo flat | Flat-rate simplicity + partnerships (REI communities) | A no-free-tier model can work but grows slower; partnerships matter |
| **Hemlane** | self-managing 1–15 units | $28 base + $2–58/unit tiers (top tier includes humans) | Hybrid software+human; premium prices for outcomes | Small landlords pay real money ($58/unit!) when outcomes, not software, are sold |
| **Latchel** | property managers | **Free for the PM; tenant-funded** ($15–18/mo resident benefit); claims +$48/door/yr NEW PM revenue | Flipped the buyer's P&L: maintenance coordination became a profit center | The PM playbook: don't sell PMs a cost, sell them margin |
| **Mezo** ($6M raised) | PM software | AI work-order triage | Sold into PM platforms; acquired by Property Meld 01/2025, now bundled **free** | Triage alone is a *feature* at the PM-platform level — it commoditizes there. Own the end relationship instead |
| **EliseAI** ($2B val.) | enterprise multifamily | Full-lifecycle AI, enterprise contracts | Lighthouse enterprise accounts (28 of top-30 owners) | The top of the market is taken. Never compete there; it validates the category |

**The structural insight:** the maintenance-triage capability is being
commoditized *inside PM platforms* (Mezo → free bundle) while remaining
**completely unserved for the self-managing landlord** — nobody owns the
"your tenant texts one number and you sleep" relationship for the 1–10
door owner. That's the wedge, and it's also why owning the tenant
relationship (our own Twilio number) matters strategically: it can't be
bundled away by someone else's platform.

## 2. Two segments, two plans

### Segment A — self-managing landlords (1–10 doors) · the beachhead

| | **Free — "First Door"** | **Pro — $29/door/mo** ($290/yr) |
|---|---|---|
| Triage + severity classification | ✔ | ✔ |
| Emergency call + escalation chain | ✔ (the safety promise is never paywalled) | ✔ |
| Tenant safety SMS | ✔ | ✔ |
| Drafts in your voice + approval queue | — (notify + raw message only) | ✔ |
| Trust ladder / routine auto-send | — | ✔ |
| Audit trail export ("LTB pack") | — | ✔ |
| History | 30 days | Unlimited |
| Doors | 1 | Unlimited at $29 each |

Rationale:
- Resolves the free-first-door flaw (the single-door majority converts on
  *convenience*, not safety): free tier still delivers the emergency
  promise — which is the word-of-mouth engine — but drafting, auto-send,
  and the LTB pack are the product.
- **$29 not $19**: Hemlane charges up to $58/unit with humans attached;
  a PM costs $150+/door. $29 reads cheap against both anchors. Pilots
  test $29 (issue #102's would-you-pay question updated accordingly).
- Free-tier COGS ≈ $2–4/door/mo (LLM classification + number rental) —
  a real but acceptable CAC, cheaper than ads.
- Volume break at 11+ doors: $19/door (bridges toward the PM tier).

### Segment B — property managers (50–5,000 doors) · the second act

**Not before small-landlord PMF** (the design-partner pilots stay
landlord-only). But the plan exists now so architecture and waitlist
decisions are deliberate:

- **Product shape:** "Stoop Desk" — after-hours + overflow triage that
  replaces answering services ($2–5/door/mo PM cost today) and feeds
  work orders into their existing PM software (Buildium/AppFolio/Rent
  Manager integrations, their app marketplaces are the distribution
  channel).
- **Pricing, the Latchel lesson:** make Stoop revenue-positive for the
  PM. Platform fee $6–10/door/mo, with an optional **resident-benefit
  model**: the PM offers tenants a $10–15/mo benefit package (instant
  24/7 response SLA, status tracking) and keeps the margin — Stoop
  becomes a profit center, which is how Latchel made "free for PMs"
  print +$48/door/yr for its buyers.
- **What it requires (why it's later):** team seats/roles, SLAs,
  integrations, SOC 2 (already a scaling trigger in architecture.md §11),
  multi-assignee escalation chains.
- **Now:** a "Property managers — join the waitlist" link on the landing
  page. Costs nothing, measures pull, builds the M4 case.

### Pricing summary

| Tier | Who | Price |
|---|---|---|
| First Door (free) | 1-door landlords | $0 — triage + emergency, forever |
| Pro | 1–10 doors | $29/door/mo or $290/door/yr |
| Portfolio | 11–50 doors | $19/door/mo |
| Stoop Desk (future) | PMs 50+ doors | $6–10/door/mo + resident-benefit revenue option |

## 3. Traction playbook (stolen from the winners, localized)

1. **The TurboTenant machine, Ontario edition.** TurboTenant's growth =
   free tools + SEO education at enormous scale — but their content is
   US-generic. Ontario-specific search space (LTB processes, N4/N5 forms,
   Ontario heat bylaws, "tenant texted me at 3am") is uncontested.
   Free lead-magnet tools that rank: LTB notice templates, a rent-receipt
   generator, an "is this a maintenance emergency?" checker (which is
   literally our rubric as a quiz — it demos the product).
2. **Freemium as distribution** (TurboTenant/Avail): the free First Door
   tier is the ad budget. Every free landlord's tenants experience the
   product working.
3. **Community partnerships** (RentRedi×BiggerPockets pattern): Ontario
   REI podcasts/meetups/SOLO — already in `design-partners.md`; extend
   with a referral mechanic (free month per referred door) once pilots
   convert.
4. **For the PM segment later:** integration marketplaces (Buildium/
   AppFolio app stores) are where PMs shop, and the ROI pitch is
   answering-service replacement + Latchel-style resident-benefit margin.
5. **What we don't copy:** EliseAI's enterprise motion (capital-intensive,
   market taken) and Mezo's sell-into-platforms motion (commoditizes the
   exact capability we differentiate on).

## 4. Decisions this locks (pending founder sign-off)

1. Free tier = capability-gated (triage+emergency), not door-count-only.
2. $29/door Pro anchor, tested in pilot retros (#102).
3. PM segment deferred to post-PMF, but waitlist link ships with the
   landing page (#112 AC addition).
4. Issues #52/#58 pricing ACs update from "$0 first door / $19" to this
   table once 1–2 are confirmed.

## Sources

- Property Meld acquires Mezo (PRNewswire, Jan 2025) — funding & bundling
- Latchel pricing/model — latchel.com, Rental Housing Journal, AAOA
- EliseAI traction — eliseai.com, Alpha Partners, BusinessWire (Zillow)
- TurboTenant freemium/SEO — turbotenant.com, CRE Daily review
- Avail/RentRedi comparisons — saasworthy, rentredi.com, KDS Development
- Hemlane pricing/positioning — hemlane.com/pricing, Capterra, KDS
