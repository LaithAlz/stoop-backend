# Outreach Scripts — design-partner recruiting

> Channel strategy in `design-partners.md`. Rules that apply to all of
> these: plain English, no "founding/cohort" language, never spammy —
> contribute first in communities, recruit second. Track every contact's
> channel in the waitlist `source` / a sheet.

---

## 1. Warm intro (text/DM to personal network) — highest conversion

> Hey [name] — you've got the rental on [street], right? I'm building
> something for exactly that: tenants text one number, software reads and
> sorts everything, drafts your replies for you to approve, and only a
> real emergency rings your phone at night. I'm taking on ten Ontario
> landlords personally at $5/month locked in for life. Want me to show
> you? Takes 10 minutes, and "no" is a fine answer.

**Ask for referrals on every no:** "All good — know any landlord who
complains about tenant texts?"

## 2. Kijiji / Marketplace FRBO landlords (after their listing closes)

> Hi [name] — saw your listing for [address] (looks rented now, congrats).
> I'm a Toronto founder building Stoop: your tenants text one number,
> software sorts everything and drafts replies in your voice for your
> approval, and real emergencies ring your phone immediately — everything
> else waits politely until morning. Early access is $5/month, locked in
> for life, and I onboard every landlord personally. If the 2 AM "is this
> urgent?" text has ever been your problem, I'd love to show you:
> [DOMAIN]/early-access. Either way — congrats on the lease.

Rules: only after the listing closes (never while they're fielding
applicants); max one follow-up a week later; personalize the address.

## 3. Reddit (r/OntarioLandlord) — contribute-first model

**Weeks 1–3: no recruiting.** Answer maintenance-triage questions
genuinely (the rubric doc makes you the most precise commenter in the
thread: what's an emergency vs what waits, Toronto's 21°C rule, how to
document everything for the LTB).

**Week 4+, one post, transparent:**

> **I built a tool that answers my tenants' texts so I can sleep — looking
> for a few Ontario landlords to try it (free pilot)**
>
> Landlord here. After one too many 11 PM "the faucet is dripping" texts
> and one real 2 AM pipe burst, I built Stoop: tenants text one number,
> software reads every message, drafts a reply in my voice that I approve
> before it sends, and *only* a genuine emergency (flood, gas, no heat in
> a freeze) rings my phone — with a real escalation chain if I sleep
> through it. Everything is logged, so there's a clean record of what was
> said and when.
>
> It's early. I'm taking ten Ontario landlords, onboarding each one
> personally, free pilot, then $5/month locked in for life. You keep
> approval over every message. If your tenant line is your personal cell
> and you've felt the 2 AM problem: [link]. Happy to answer anything here,
> including "why wouldn't I just keep texting them myself?"

(That last line invites the objection thread — answer it honestly and
the thread does the selling.)

## 4. REI meetup — the 30-second version + demo

> "I make tenant texts not your problem. Tenants text one number; the
> software sorts it, drafts your reply, you tap approve. Real emergencies
> ring your phone — a dripping faucet waits until morning. Here —" [hand
> them your phone with /early-access demo] "— text it something a tenant
> would say."

The live demo on *their* message is the close. Collect a number, not a
maybe.

## 5. Follow-up cadence (all channels)

- Verbal yes → same-day text with the one-page pilot terms + a proposed
  onboarding slot. Strike while warm.
- No reply → exactly one follow-up at +5–7 days ("floating this back up —
  spots are going to people with the noisiest tenants first 🙂"), then stop.
- Every contact logged: name, channel, doors, stage
  (contacted/replied/qualified/yes/onboarded), next action + date.

## 6. Qualification (from design-partners.md, as natural questions)

"How many doors?" · "Do tenants text your personal cell today?" ·
"Self-managing or PM?" · "Ontario?" · "Would you be okay telling tenants
a software assistant helps you respond?" — 4+ green answers = offer the
pilot on the spot.

---

## 7. Referral mechanic (active from first happy pilot)

**The offer:** give a month, get a month — the referred landlord gets
their first paid month free; the referrer gets a free month per landlord
who activates (sends real tenant traffic, not just signs up). Stacking
allowed; a landlord who brings five friends rides free for five months —
that outcome is a *win* (five new accounts at near-zero CAC vs. ~$15 in
credits).

**The ask (the only script that matters), used at two moments —
end of a happy weekly check-in, and right after a visible save (an
emergency handled, a brutal week summarized):**

> "Glad it's working. Do you know one other landlord whose tenants text
> their personal cell? If they sign up through your link you both get a
> month free — and they get the locked $5 rate while it lasts."

**Mechanics v1 (no code):** personal referral note in the waitlist
`source` field ("ref:<landlord-id>"); founder applies credits manually in
Stripe (volume makes this trivial for the first year). Productize (unique
links, auto-credit) only when manual hurts — it's a Train 3 nicety.

**Measurement:** referred-in count per landlord; % of new signups with a
ref source. The 3-year plan's H1 gate ("a repeatable channel") is
satisfied by referral alone if ref-source exceeds ~30% of signups.
