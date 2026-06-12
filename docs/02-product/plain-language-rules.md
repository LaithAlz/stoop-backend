# Plain-Language Rules — every message Stoop sends

> Written 2026-06-12 from the founder principle: **assume the reader is
> panicked, half-asleep, reading on a cracked phone, or speaking English
> as a third language — and design for that person.** These rules bind
> `draft_response` (#33), the emergency safety templates (#108), the
> holding ack (#109), and vendor messages (#115). The eval grader (#35)
> enforces the testable ones.

## Who never sees an app (by design)

Tenants interact with Stoop through **SMS only**. No app, no portal, no
account, no link they must tap to "view the full message." If a flow ever
requires a tenant to do anything other than read a text and reply to it,
the flow is wrong.

## Rules for tenant-facing messages

1. **Grade-5 reading level.** Short words, short sentences (≤ 15 words),
   active voice. "Turn off the breaker" not "the breaker should be
   deactivated."
2. **One instruction per message-beat.** In an emergency, instructions
   come as a numbered list, most important first, max three steps.
   Nobody executes step 4 during a flood.
3. **No jargon, no idioms.** "Shut-off valve" gets a location ("under the
   sink, turn it right"). No "touch base," "loop in," "ASAP." ESL tenants
   are a core audience in Ontario.
4. **Concrete over relative.** "Tony will come Thursday morning between
   8 and 11" — never "later this week," never "soon."
5. **Length budget:** routine replies ≤ 2 SMS segments (~300 chars);
   emergency safety messages ≤ 3 short numbered lines. Walls of text are
   a failure mode, not thoroughness.
6. **One question at a time.** Clarifying questions (#66) never stack:
   "Is water still coming out?" — wait — then the next question.
7. **Tone under stress: calm, warm, certain.** Never scolding, never
   panicked, never legalistic. The reader's stress level is the input;
   the message's calm is the output.
8. **Typos, caps, slang, fragments are valid input.** The agent never
   corrects, comments on, or mirrors a tenant's writing — it just
   understands it. (The eval suite already tests ALL-CAPS panic and
   hedged minimization; add fragment/typo variants as the corpus grows.)
9. **Photos welcome, never required.** "Send a photo if you can" — the
   flow must work identically if they can't.

## Rules for landlord-facing surfaces

1. **One primary action per screen.** The queue's job is the approve
   button. Everything else is one tap deeper.
2. **The app is optional for the core loop.** Approving must work from:
   (a) the push notification, (b) **an SMS reply** — see below, (c) the
   dashboard. The least technical landlord in Ontario can run Stoop
   without ever opening the app.
3. **No settings maze.** Anything configurable has a sane default chosen
   at onboarding; the settings page is one screen, forever.
4. **Numbers the landlord sees are explained where they appear** ("94/100
   — six more clean approvals to unlock"), never bare metrics.

## Approve-by-SMS (the lowest-tech approval channel)

When a draft is ready, the landlord's notification SMS reads:

> Stoop: Maria (Unit 2, Palmerston) reported no heat. Draft ready:
> "Hi Maria — so sorry…[first 200 chars]…"
> **Reply 1 to send · 2 to skip · or open the app to edit.**

- Reply `1` → same path as dashboard approve (5-min window to text
  `UNDO`, since SMS has no undo bar). Reply `2` → rejected, case stays
  open. Anything else → treated as a question for the founder/support in
  v1 (logged, never silently dropped).
- Same trust-metric, audit, and stale-draft semantics as the API
  (`../03-engineering/api-contracts.md`) — this is a third client of the
  approve endpoint, not a new pathway.
- Edits still require the app (typing a full reply over SMS is worse UX,
  not better).

## The recap templates (the two scheduled messages)

**Morning summary SMS (7:00 AM, only if something waits):**
> Stoop: 2 things waiting for your OK — Maria (no heat, drafted) and Sam
> (faucet, Tony suggested Thursday). Reply 1 to send Maria's, or open the
> app. Nothing was an emergency overnight.

**Nightly recap (9:30 PM, only if something happened):**
> Stoop: today I handled 3 messages. Sent for you: Sam's parking question.
> Waiting on: Tony to confirm Thursday. Your phone stayed quiet — nothing
> was urgent. Details in the app.

Rules: skipped entirely on empty days (silence is the product), never
more than 2 segments, names not unit numbers, and "nothing was an
emergency" stated explicitly — reassurance is the payload.

## Enforcement

- `draft_response` and the safety templates carry these rules in their
  prompts; the eval grader (#35) checks length budgets, numbered-step
  format for emergencies, and bans a jargon list.
- Reading-level: automated check (Flesch-Kincaid ≤ grade 6) as a soft CI
  warning on template changes; LLM-judge for tone.
- These rules are versioned with the prompts — changing them is a prompt
  version bump + eval run, like everything else.
