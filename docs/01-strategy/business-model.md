# Business Model — segments, pricing, and the traction playbook

> **Status:** Draft for founder review, 2026-06-11. Based on competitive
> research (sources at bottom) + the unit-economics work in
> `architecture.md` §9 / issue #111.

## 1. What the competition teaches

| Company | Segment | Model | Traction mechanism | Lesson for Stoop |
|---|---|---|---|---|
| **TurboTenant** | 1–10 door landlords | Freemium; ~$149/yr premium; tenant-paid screening fees | Free core tools + massive SEO/education machine; 1M+ landlords | Freemium + content WORKS in this market; monetization can sit on premium convenience |
| **↳ TurboTenant Maintenance AI / Autopilot** | same base, moving into our lane | *Maintenance AI* (launched Oct 27, 2025): an AI first-responder bundled into TurboTenant's existing maintenance-tracking feature — asks tenants diagnostic questions, gathers photos/details, guides simple self-diagnosis before the landlord sees it. Pricing tier not publicly disclosed; appears to ride the free/premium suite rather than a separate paid add-on. *Autopilot* (launched Jan 20, 2026): a **separate, paid** flat-fee full-service offering — tenant placement, showings, day-to-day communication, *and* maintenance coordination; **$250/mo + $100/mo per additional property** per TurboTenant's own support pages (9,000+ tenants placed, 40,000+ repairs coordinated claimed at launch) | Riding the same SEO/freemium machine into maintenance specifically | TurboTenant is now actively building AI maintenance triage into its free suite — the "nobody does this" framing is no longer true; the wedge has to be the SMS-only/approval-first/owned-number combination, not the triage idea alone |
| **Avail** (→ Realtor.com) | small landlords | Free unlimited units; $9/unit/mo plus tier | Freemium + marketplace distribution; acquired for distribution | Per-unit premium at single-digit $ is accepted |
| **RentRedi** | small landlords | Paid-only, ~$30/mo flat | Flat-rate simplicity + partnerships (REI communities) | A no-free-tier model can work but grows slower; partnerships matter |
| **Hemlane** | self-managing 1–15 units | $28 base + $2–58/unit tiers (top tier includes humans) | Hybrid software+human; premium prices for outcomes | Small landlords pay real money ($58/unit!) when outcomes, not software, are sold |
| **↳ Hemlane Essential** | mid-tier, self-managing landlords who want maintenance off their plate | $48/mo (Basic $30 → Essential $48 → Complete $86; $28 base + $20/unit at Essential) — adds a dedicated repair coordinator handling requests 24/7 including emergencies, automatic diagnosis/troubleshooting, work-order management, no-markup invoicing, on top of Basic's software features | Sits between pure-software Basic and full-service Complete | Confirms real willingness-to-pay for "maintenance handled for me" well above our $10 — but Hemlane routes through a human coordinator, not an owned tenant-SMS relationship |
| **Rentyn** (Canada) | landlord portfolios, per-unit | $49/mo Starter (≤5 units) → $149/mo Portfolio (≤25 units) → $299+/mo Operator ($6–8/unit past 25); annual billing gets 20% off | AI operating desk sold direct: 24/7 AI calls + texts, maintenance triage, emergency escalation, portfolio memory | Closest positioning match to Stoop (AI intake + triage + escalation) — but phone-first (not SMS-only), per-unit from $49/mo, and no free tier; validates the category in Canada without owning our specific wedge |
| **Latchel** | property managers | **Free for the PM; tenant-funded** ($15–18/mo resident benefit); claims +$48/door/yr NEW PM revenue | Flipped the buyer's P&L: maintenance coordination became a profit center | The PM playbook: don't sell PMs a cost, sell them margin |
| **Mezo** ($6M raised) | PM software | AI work-order triage | Sold into PM platforms; acquired by Property Meld 01/2025, now bundled **free** | Triage alone is a *feature* at the PM-platform level — it commoditizes there. Own the end relationship instead |
| **EliseAI** ($2B val.) | enterprise multifamily | Full-lifecycle AI, enterprise contracts | Lighthouse enterprise accounts (28 of top-30 owners) | The top of the market is taken. Never compete there; it validates the category |

**The structural insight:** the maintenance-triage capability is being
commoditized *inside PM platforms* (Mezo → free bundle) — that part of
the thesis holds. What's no longer true is "unserved": TurboTenant shipped
Maintenance AI into its free suite (Oct 2025) plus a paid Autopilot
full-service tier (Jan 2026), Hemlane sells maintenance coordination as a
hybrid software+human plan, and Rentyn is selling AI intake/triage/
escalation direct to Canadian landlords from $49/mo. The self-managing
1–10 door segment is actively being served by adjacent entrants — nobody
owns it uncontested. So the wedge isn't "nobody does this"; it's the
specific combination none of them ship together: tenant-side is **pure
SMS to one number** (no app, no portal, no login), drafts are
**approval-first, in the landlord's own voice** (never autonomous by
default), the emergency line is **free and always-on**, and we **own the
tenant relationship** on our own Twilio number so it can't be bundled away
by someone else's platform.

## 2. Two segments, two plans

### Segment A — self-managing landlords (1–10 doors) · the beachhead

| | **Free — "Safety Net"** | **Early-access rate — $5/month flat** (per landlord, up to 10 doors; standard price $10/mo) |
|---|---|---|
| Triage + severity classification | ✔ | ✔ |
| Emergency call + escalation chain | ✔ (the safety promise is never paywalled) | ✔ |
| Tenant safety SMS | ✔ | ✔ |
| Drafts in your voice + approval queue | — (notify + raw message only) | ✔ |
| Trust ladder / routine auto-send | — | ✔ |
| Audit trail export ("LTB pack") | — | ✔ |
| History | 30 days | Unlimited |
| Doors | 1 | Up to 10 doors — one flat price (founder decision: flat, not per-door, 2026-06-11) |

Rationale:
- Resolves the free-first-door flaw (the single-door majority converts on
  *convenience*, not safety): free tier still delivers the emergency
  promise — which is the word-of-mouth engine — but drafting, auto-send,
  and the LTB pack are the product.
- **Penetration pricing (founder decision 2026-06-11):** launch at
  $5/month flat (per landlord, ≤10 doors) — accepting thin early margin to
  maximize adoption. The escape hatch is **grandfathering**: founding
  landlords keep $5/month for life; later cohorts pay the target price
  ($10 flat — founder decision 2026-06-11 — sanity-checked by Van Westendorp in pilot retros, #102). Prices
  ratchet up by cohort, never down. "Early-access rate" framing (customer-facing language never says "founding"/"cohort" — it signals small scale; founder decision 2026-06-11)
  signals the real price is higher — it reads as early access, not as
  cheap.
- Free-tier COGS ≈ $2–4/door/mo (LLM classification + number rental) —
  a real but acceptable CAC, cheaper than ads.
- Stripe-fee note: at $5 charges, fees eat ~9%. Nudge annual ($50/door/yr)
  at checkout to cut both fees and churn.

### Segment B — property managers (50–5,000 doors) · the second act

**Not before small-landlord PMF** (the design-partner pilots stay
landlord-only). But the plan exists now so architecture and waitlist
decisions are deliberate:

- **Product shape:** "Stoop Desk" — after-hours + overflow triage that
  replaces answering services ($2–5/door/mo PM cost today) and feeds
  work orders into their existing PM software (Buildium/AppFolio/Rent
  Manager integrations, their app marketplaces are the distribution
  channel).
- **Pricing, the Latchel lesson (founder-confirmed):** platform fee
  $1.50–2/door/mo — matching Latchel's actual basic pricing ($25 +
  $0.80/unit) because PMs price-shop software ruthlessly — with the
  **resident-benefit model** as the real revenue: the PM offers tenants
  a $10–15/mo benefit package (instant 24/7 response SLA, status
  tracking) and keeps the margin. Stoop becomes a profit center, which
  is how Latchel made "free for PMs" print +$48/door/yr for its buyers.
- **What it requires (why it's later):** team seats/roles, SLAs,
  integrations, SOC 2 (already a scaling trigger in architecture.md §11),
  multi-assignee escalation chains.
- **Now:** a "Property managers — join the waitlist" link on the landing
  page. Costs nothing, measures pull, builds the M4 case.

### Pricing summary

| Tier | Who | Price |
|---|---|---|
| Safety Net (free) | any landlord | $0 — triage + emergency calls, forever |
| Early-access rate | pilot + early customers | $5/month flat (≤10 doors), $50/yr — grandfathered for life |
| Standard | 1–10 doors | **$10/month flat** (founder-set 2026-06-11); structure revisited with pilot data |
| Stoop Desk (future) | PMs 20+ doors | $1.50–2/door/mo platform fee + resident-benefit revenue (the Latchel model — margin lives in the benefit package, not the platform fee) |

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

1. ✅ Free tier = capability-gated (triage+emergency), not door-count-only.
2. ✅ **$5/month FLAT early-access rate, $10 standard** (founder decisions 2026-06-11: penetration
   pricing, thin early margin accepted, grandfathered for life; target
   $10–15 for later cohorts, validated via #102 Van Westendorp).
3. ✅ PM segment deferred to post-PMF at $1.50–2/door + resident benefit;
   waitlist link ships with the landing page (#112).
4. ✅ Issues #52/#58/#102/#112 ACs updated to this table (2026-06-11).

## Sources

- Property Meld acquires Mezo (PRNewswire, Jan 2025) — funding & bundling
- Latchel pricing/model — latchel.com, Rental Housing Journal, AAOA
- EliseAI traction — eliseai.com, Alpha Partners, BusinessWire (Zillow)
- TurboTenant freemium/SEO — turbotenant.com, CRE Daily review
- Avail/RentRedi comparisons — saasworthy, rentredi.com, KDS Development
- Hemlane pricing/positioning — hemlane.com/pricing, Capterra, KDS
- TurboTenant Maintenance AI launch (Oct 27, 2025) — [PR Newswire](https://www.prnewswire.com/news-releases/turbotenant-unveils-maintenance-ai-a-smarter-way-to-manage-rental-property-repairs-302594383.html), turbotenant.com/product-updates/introducing-maintenance-ai
- TurboTenant Autopilot launch (Jan 20, 2026) — Access Newswire via [Yahoo Finance](https://finance.yahoo.com/news/turbotenant-launches-autopilot-flat-fee-140000977.html), digitaljournal.com; pricing per [TurboTenant support](https://support.turbotenant.com/en/articles/autopilot-pricing-and-billing)
- Hemlane Essential tier pricing (Basic $30 / Essential $48 / Complete $86) — hemlane.com/pricing
- Rentyn pricing & features (Starter $49/mo, Portfolio $149/mo, Operator $299+/mo) — rentyn.ca
