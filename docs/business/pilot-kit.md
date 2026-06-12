# Pilot Kit — tenant disclosure, pilot agreement, onboarding script

> Operational documents for design partners (`design-partners.md`).
> Disclosure + agreement should get the same lawyer pass as the ToS.

---

## 1. Tenant disclosure template (the landlord sends this — required)

**SMS version (preferred — it's the channel they'll use):**

> Hi [name], it's [landlord]. Quick heads-up: I've set up a new number for
> anything about the apartment — repairs, questions, anything: [STOOP
> NUMBER]. Save it as "[Building] Maintenance." A software assistant helps
> me read and respond faster (I still see and approve everything), and if
> something's ever a real emergency it gets to me immediately, day or
> night. Texting works exactly like texting me.

**Letter/email version (for files):**

> I now use a service called Stoop to manage maintenance messages for
> [address]. When you text [NUMBER], software helps me sort and respond to
> messages quickly — I review and approve responses, and urgent issues
> reach me immediately at any hour. Your messages are stored securely in
> Canada and used only to manage maintenance for this property. Questions:
> just ask, or see [DOMAIN]/privacy.

Rules: send before routing tenant messages through Stoop; keep a copy;
the onboarding wizard (#113) has a confirm-sent checkbox for exactly this.

---

## 2. Pilot agreement (one page — plain English on purpose)

**Stoop Early Access Pilot — [Landlord name], [date]**

**What you get**
- Stoop live on up to [N] properties: every tenant text read and sorted,
  replies drafted in your voice for your approval, emergency phone calls
  with escalation, vendor coordination.
- Free during the pilot ([8] weeks), then $5/month locked in for as long
  as you stay — the early-access rate.
- Laith personally onboards you (~30 min) and is directly reachable.

**What we ask**
- Send the tenant notice (above) before go-live. Non-negotiable.
- Real usage: your actual tenant line, not a test number.
- 15 minutes of feedback weekly for the first month.
- Permission to review message handling for quality (we look at
  classifications to improve them; we treat content as confidential) and
  to turn anonymized mistakes into test cases.
- A testimonial at the end **only if you're happy**.

**The honest parts**
- Stoop is early software. Sorting and drafting can make mistakes; you
  approve before anything sends, and emergencies fail toward calling you.
  Stoop is not an emergency service — 911 remains the answer for danger
  to life or property.
- Cancel anytime with a text. Your number can be released to you, and you
  get an export of every conversation.
- Either of us can end the pilot; no fees either way.

Signed: ____________ (landlord) ____________ (Stoop / [ENTITY])

---

## 3. Onboarding session script (30 min, founder-led, cohorts of 3)

**Before the call:** account pre-created; their properties entered from
the qualification chat; Twilio number provisioned.

1. **(5 min) The promise, restated:** "Tenants text one number. You
   approve everything. Only a real emergency rings you. Tonight your
   phone is quiet."
2. **(5 min) Voice profile:** paste 3–5 of their *real* past replies to
   tenants ("forward me the last few texts you sent tenants"). Pick tone.
3. **(5 min) House rules & people:** parking, garbage, quiet hours; their
   plumber/electrician/handyman names + numbers; backup contact for the
   escalation chain ("who do we call if you sleep through it?").
4. **(5 min) The dry run:** founder texts the number as a fake tenant —
   "my faucet is dripping". Watch the draft arrive; they approve it on
   their phone; show undo.
5. **(5 min) The emergency demo:** "WATER POURING THROUGH CEILING" —
   watch their phone ring. This moment sells the next 6 months.
6. **(5 min) Tenant notice:** they send the disclosure SMS to tenants
   *on the call* (the wizard checkbox). Schedule the week-1 check-in.

**After:** founder watches every classification in LangSmith for 2 weeks
(design-partners.md pre-mortem). Log every friction point as an issue
same day.

**Weekly check-in template (15 min):** any message handled wrong? · any
moment you didn't trust it? · what did you do outside Stoop that Stoop
should have done? · NPS 0–10 + why. (Retro call at week 8 runs the
pricing study per #102.)
