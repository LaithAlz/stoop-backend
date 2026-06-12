# Waitlist Nurture — what happens after the email lands

> Written 2026-06-12 to close the leakiest funnel gap: signups going cold
> during the build. Tone rules apply: founder-personal, plain English,
> no "founding/cohort", no hype. Every email is from Laith, signed Laith.

## Mechanics (honest about scale)

- **Under ~50 signups: send manually.** Personal email, individually
  addressed (the D1 table has their email + doors + PM flag). A founder
  who writes you a real two-line email converts better than any sequence
  — and we promised "no newsletter."
- **Past ~50:** move to a simple sender (Resend — already penciled for
  dunning later) with the same copy. Never a marketing-platform footer.
- Reply-to is always Laith's real inbox. Replies are the actual goal —
  every reply is a qualification conversation (see
  `outreach-scripts.md` §6).
- PM-flagged signups get the PM variant (below), not the landlord track.

## Touch 0 — instant/same-day welcome (the only "automated-feeling" one)

**Subject:** you're in — and a quick question

> Hi — Laith here, the person building Stoop. Thanks for putting your
> email in. Two things:
>
> 1. You're on the early-access list at $5/month, locked in for as long
> as you stay. That's real — when prices go up later, yours doesn't.
>
> 2. The quick question: what does your tenant messaging look like today?
> Texts to your personal cell? How many doors? Hit reply — I read and
> answer everything personally. The first people on get onboarded by me
> directly.
>
> — Laith

*Goal: a reply. A reply = qualified pilot candidate = the design-partner
funnel, weeks early.*

## Touch 1 — at ~2 weeks (build update)

**Subject:** what your $5 is buying (build update)

> Quick honest update, since you trusted me with your email:
>
> The core is coming together — tenant texts in, sorted by urgency,
> replies drafted in your voice for your approval, and real emergencies
> ring your phone with a backup plan if you sleep through it. [One
> screenshot of the real approval queue.]
>
> One thing I'd love from you (optional): forward me the most annoying
> tenant text you've ever gotten. I'm testing the sorting against real
> messages, and yours is probably better than my test data.
>
> — Laith

*Goal: engagement + real test data (anonymize before use; these become
eval-scenario candidates).*

## Touch 2 — at pilot-readiness (the conversion email)

**Subject:** Stoop is live — want one of the first spots?

> It works. A tenant texted "the heat stopped working" at 2 AM last week
> and the right things happened: sorted as urgent-not-emergency, reply
> drafted, my phone stayed dark, approved with coffee at 7.
>
> I'm onboarding the first landlords personally now — 30 minutes, I do
> the setup with you, your tenants just keep texting like they always
> have. Free for the pilot weeks, then your locked $5/month.
>
> Want a slot? Reply with a day that works. If now's not right, no
> problem — your rate stays locked either way.
>
> — Laith

*Then: the pilot-kit onboarding flow (`../06-legal/pilot-kit.md`).*

## Touch 3 — at public launch (for non-converters only)

Short note: it's open, your rate is still locked, one-click start, plus
one real (permissioned) pilot quote. After this: at most quarterly
updates. We promised no newsletter; keep the promise.

## PM variant (is_pm = 1)

Touch 0 swaps paragraph 1: "You're on the Stoop Desk list — after-hours
coverage built for portfolios, from $1.50/door. We're starting with
self-managing landlords and building the PM product with the teams on
this list — so the most useful thing you can do is reply with: how many
doors, what software you run on (Buildium/AppFolio/other), and what
after-hours costs you today." Their answers literally choose the first
integration (`../04-roadmap/release-train.md`, Train 3 trigger).

## Measurement

Track per touch (manually at first, PostHog/Plausible later): sent,
replies, pilot conversions. The number that matters is **replies to
Touch 0** — it predicts pilot supply weeks ahead.
