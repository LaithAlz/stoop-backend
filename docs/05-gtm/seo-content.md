# SEO Content — first two posts + the emergency checker

> Strategy: `business-model.md` §3 — the TurboTenant machine, Ontario
> edition. These target searches with real volume and zero good Ontario
> answers. Publish under /[blog|guides]/ on the production domain; each
> ends with the same soft CTA. Tone: written by a landlord who knows the
> rules, not by a content farm. No "triage", no legalese.

---

## Post 1 — "My tenant texted me at 3 AM. What actually counts as an emergency?"

Target searches: *tenant texted me at night · tenant emergency what to do
landlord · is no heat an emergency Ontario · landlord 3am call*

Outline (~1,200 words):
1. **The scenario** — the 3 AM text, verbatim-style: "the heat stopped
   working and we have the baby this week." Your half-asleep brain has to
   make a judgment call. Here's the framework so you don't have to invent
   one at 3 AM.
2. **The two questions that decide everything** (this is rubric v1.0,
   humanized): Is anyone or anything in danger *right now*? Can this
   genuinely wait until morning without getting worse?
3. **Ring-the-alarm list:** active water where it shouldn't be, gas smell
   (out of the unit, then call the utility), fire/smoke (911 first,
   always), no heat in a deep freeze, a unit that can't be locked.
4. **The "feels urgent, waits until 7 AM" list:** no heat on a mild
   night, dead fridge, the only toilet, lockouts in decent weather — act
   fast in the morning, lose no sleep tonight.
5. **The 80%:** drips, parking, receipts, noise — never a night problem.
6. **Ontario specifics:** Toronto's 21 °C rule (Sept 15–Jun 1), vital
   services, why "document everything with timestamps" is the best advice
   nobody takes.
7. **Soft close:** "I built Stoop because I got tired of being the
   3 AM judgment call. Tenants text one number; software sorts it using
   exactly this framework; real emergencies ring my phone and everything
   else waits until coffee. Early access: [link]."

## Post 2 — "The paper trail: how to document tenant maintenance (before you need it)"

Target searches: *landlord documentation maintenance Ontario · LTB
maintenance dispute evidence · tenant repair request records*

Outline (~1,000 words):
1. The uncomfortable truth: maintenance disputes are decided on records,
   and texts scattered across a personal phone are terrible records.
2. What a clean record looks like: every request, timestamped; every
   response, timestamped; what was done and when; photos attached to the
   issue, not lost in a camera roll.
3. The gap that kills landlords: the *response time* between report and
   action — if you can't show it, it didn't happen.
4. Practical systems, honestly compared: a dedicated notebook (fails),
   email-only policy (tenants won't), spreadsheet (you won't), a
   dedicated line + tooling (works because it's automatic).
5. Soft close: every Stoop conversation is an automatic, exportable,
   timestamped record — "the folder you hope you never need, built while
   you sleep."

---

## The Emergency Checker (interactive tool — the rubric as a quiz)

**Route:** /is-it-an-emergency · **Goal:** rank for "is X a landlord
emergency", demo the product mechanic, capture emails softly.

Build: ~10 multiple-choice steps mirroring the Tier-0/rubric logic, pure
client-side (it must never look like real advice infrastructure):

1. "What's happening?" → water / heat / electrical / gas-smell / locks &
   security / appliance / noise / other
2. Per-branch follow-ups (water: "actively flowing or dripping?";
   "touching electrical fixtures?"; heat: "what's the outdoor temp
   tonight?"; "anyone vulnerable in the unit — infant, elderly, medical
   equipment?")
3. Verdict card, in product language:
   - 🔴 **Handle it now** — "This is a wake-up-the-landlord situation.
     [If fire/gas/medical: Call 911 / leave and call the gas utility
     first.]"
   - 🟠 **First thing tomorrow** — "Real, urgent, and safe to sleep on.
     Line up the fix at 7 AM."
   - 🟢 **The routine pile** — "Schedule it this week."
4. Under every verdict: "Stoop makes this call automatically on every
   tenant text — and only the red ones ring your phone. [Get early
   access]" + optional email field ("send me the framework as a PDF").
5. Disclaimer line: educational tool, not emergency guidance; danger to
   life or property = 911.

**Measurement:** Plausible events `checker_completed` (with verdict) and
`checker_email_captured`. The verdict distribution is also a free survey
of what landlords worry about — feed it back into content topics.

---

## Cadence & mechanics

- 2 posts/month minimum; every post answers ONE search phrase an Ontario
  landlord actually types, with the specifics (temps, bylaws, timelines)
  US content can't fake.
- Future topics queue: "Tenant won't let the plumber in" · "Rent receipt
  rules in Ontario (free generator)" · "What to do in the first hour of a
  burst pipe" · "Heat complaints: the landlord's calendar" · "Handing
  tenants to a property manager vs. tools: real costs".
- Each post: FAQ schema markup, one internal link to the checker, one to
  /early-access. No keyword stuffing — these win by being the only
  *correct* answer, not the loudest.
