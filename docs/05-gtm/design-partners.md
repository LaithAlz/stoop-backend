# Design-Partner Plan — recruiting the first 10 landlords

> **Status:** Draft for founder review, 2026-06-11.
> **Why now:** M2's gate needs strangers using the product, and recruiting
> Ontario landlords will take longer than building M1. Outreach starts
> during the M1 build, not after it.

## Target profile

**Yes:** Ontario (GTA-first), self-managing, 1–10 doors, texts with tenants
from a personal cell, has been woken by a tenant call at least once (the
pain is felt, not theoretical), comfortable enough with a phone to approve
drafts from one.

**No (politely):** property-management companies (different product,
different sales motion), 50+ unit professionals (want SOC 2 and SLAs),
landlords whose tenants don't text (paper-notice buildings), anyone outside
Ontario in v1 (the rubric's bylaw references are Ontario-specific).

**Qualification checklist (ask in the first conversation):**
self-manages? · texts tenants today? · door count 1–10? · Ontario? ·
willing to give tenants a new number (or forward)? · willing to tell
tenants an AI assists with replies? · 15 min/week for feedback in month 1?

## Where they are (channels, in priority order)

1. **Personal network** — everyone knows a landlord. Ask directly; warm
   intros convert 10× cold.
2. **Kijiji / Facebook Marketplace FRBO listings** — landlords posting
   rentals *themselves* are self-managing by definition, and they're
   reachable right now. Contact politely after their listing closes
   (not while they're fielding applicants).
3. **r/OntarioLandlord** — active, pain-rich. Engage genuinely first
   (answer maintenance-triage questions), recruit second. No drive-by spam.
4. **Facebook groups** — "Ontario Landlords", GTA/Durham/Hamilton REI
   groups. Same rule: contribute, then recruit.
5. **Small Ownership Landlords of Ontario (SOLO)** and the **Landlord's
   Self-Help Centre** orbit — their audience is exactly the profile.
6. **REI meetups** (Toronto, Hamilton, Durham; REITE club events) — best
   for the live demo: the landing page's interactive triage demo on a phone
   is the pitch.
7. **LTB paralegals** — they spend all day with overwhelmed landlords;
   the audit-trail feature is genuinely useful to them. Referral
   relationship, not a sale.
8. **Investment-property realtors and mortgage brokers** — every closing
   creates a new self-managing landlord.

## The pitch (60 seconds, warm or cold)

> "I'm building Stoop — your tenants text one number, an AI reads every
> message, figures out how bad it actually is, and drafts the reply in your
> voice. Nothing sends until you tap approve. If it's a real emergency —
> burst pipe, gas — it calls you immediately. Everything else waits until
> morning. I'm looking for ten Ontario landlords to pilot it free. You keep
> total control — every reply waits for your approval — and you get me
> personally onboarding you and fixing whatever annoys you, usually the
> same week."

The demo is the close: show the landing-page triage demo, then the
dashboard's 2 AM no-heat case. "This is your phone *not* ringing."

## The pilot offer (both directions explicit)

**They get:** free during the pilot + 6 months free after GA · founder does
onboarding live (30 min) · weekly 15-min call in month 1, async after ·
direct line to the founder · cancel anytime, their Twilio number ports out
with them.

**They give:** real tenant traffic · honest feedback on a weekly cadence ·
permission to turn misclassifications into (anonymized) eval cases ·
a testimonial *if and only if* they're happy at the end.

**They must do:** tell their tenants ("my maintenance line now uses an
assistant that helps me respond faster; I approve everything") — disclosure
language provided by us, non-negotiable. (PIPEDA + basic decency; also
protects the landlord.)

## Funnel math and timeline

Working backwards from 10 active pilots at M2:
~10 active ← ~15 verbal yes (drop-off is real) ← ~30 qualified
conversations ← ~100 first contacts.

- **M1 build weeks 1–2:** personal-network asks + start genuine
  participation in r/OntarioLandlord and the FB groups. Goal: 5
  conversations from warm intros.
- **M1 weeks 3–5:** Kijiji/FRBO outreach (10/week), one REI meetup
  attended with the demo. Goal: 30 cumulative conversations, 10 verbal yes.
- **M1 gate week:** the walking-skeleton demo video goes to every verbal
  yes: "it's real, you're first in line."
- **M2 start:** onboard in cohorts of 3 (white-glove takes time); weekly
  calls; every misclassification → eval case same week.

## What kills pilots (pre-mortems)

- **A missed emergency in week 1** → trust gone, word spreads in exactly
  the communities recruited from. Mitigation: pre-filter + bias rule +
  founder watches every classification for the first two weeks (volume is
  tiny; do it manually in LangSmith).
- **Tenant backlash to the AI disclosure** → soften with "I approve every
  reply" framing; track whether any tenant actually objects (hypothesis:
  near zero — tenants care about response speed, not authorship).
- **Pilot landlord goes quiet** → the weekly call is the heartbeat; two
  missed calls = ask directly if they want out. A dead pilot teaches
  nothing and blocks a slot.

## Open judgment calls for founder

1. Free-for-6-months post-pilot — generous on purpose (testimonials and
   eval data are worth more than $19/door now). Confirm or shorten.
2. Cohorts of 3 vs all-at-once onboarding — I assumed cohorts.
3. Whether the founder's own properties count as pilot #0 (I assume yes —
   you should be tenant-zero of your own product through all of M1).
