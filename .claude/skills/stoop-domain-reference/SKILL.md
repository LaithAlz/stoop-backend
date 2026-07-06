---
name: stoop-domain-reference
description: >
  Stoop domain knowledge pack (AI tenant-maintenance handling for Ontario
  landlords). Load for: severity classification (EMERGENCY / URGENT /
  ROUTINE), the frozen severity rubric v1.0 + checksum doctrine, the founder
  judgment calls (911-first, -10 °C no-heat line, vulnerable occupants, never
  round down), the five refusal flags (access_codes, legal_rent_ltb,
  cost_compensation, other_tenants, impersonation) + the code-appends
  deferral architecture, the LTB (Landlord and Tenant Board) and why Stoop
  never takes legal or rent positions, CASL / A2P 10DLC registration, SMS
  plain-language rules (grade-5, one question, 300-char budget, no
  truncation), the Tier-0 emergency prefilter (triggers/guards/
  continuous-alarm veto, monotonic-additive), the trust ladder / auto-send,
  the Open-Meteo weather lookup, or Supabase/Supavisor platform behavior
  (DuplicatePreparedStatementError, transaction-pooler knobs,
  postgres-is-not-superuser, pg_has_role trap, RLS ENABLE-vs-FORCE).
  Background theory — a reference, not a runbook.
---

# Stoop domain reference

Stoop lets tenants text one SMS number; the system classifies every message,
drafts replies in the landlord's voice, and rings the landlord's phone only
for a true emergency. This skill is the domain theory a generalist engineer
lacks: the safety model, the Ontario tenancy context, the SMS medium, and the
Supabase platform facts. Everything here is grounded in the repo docs and code
cited inline; volatile facts are date-stamped.

Repo root: the monorepo containing `apps/api` (FastAPI backend) and `docs/`
(**docs are the source of truth — code follows docs, never vice versa**).

## When NOT to use this skill

| You actually need… | Load instead |
|---|---|
| How to ship a change (gates, reviewers, never-break rules) | `stoop-change-control` |
| Symptom → triage for a failure you're seeing now | `stoop-debugging-playbook` |
| The incident history behind a rule ("why is this here?") | `stoop-failure-archaeology` |
| Load-bearing design decisions and invariants | `stoop-architecture-contract` |
| Config axes, env vars, boot gates | `stoop-config-and-flags` |
| Setting up the dev environment | `stoop-build-and-env` |
| Running migrations / operating the live DB | `stoop-run-and-operate` |
| Test/eval discipline and what counts as evidence | `stoop-validation-and-qa` |
| Copy rules for public pages, docs-of-record discipline | `stoop-docs-and-writing` |

This skill is the ONE home for rubric doctrine, Ontario/SMS/LLM-safety
theory, and Supabase platform facts — siblings cite it, don't duplicate it.

## The severity model and the frozen-rubric doctrine

Every inbound tenant message is classified into exactly one of three
severities, plus an independent **refusal overlay** (flags set regardless of
severity — see the refusal section below).

| Severity | What happens | Auto-send? |
|---|---|---|
| EMERGENCY | Landlord's phone is called immediately; tenant gets safety instructions **without waiting for approval** | Safety instructions only (the one exception to approval-first) |
| URGENT | Reply drafted immediately, top of the landlord's queue | **Never**, regardless of trust level |
| ROUTINE | Drafted from house rules and context | Only where that property has earned routine autonomy (trust ladder, unbuilt — below) |

The rubric's core is a two-question test applied in order:
**Q1** — active or imminent danger to people or property? YES → EMERGENCY.
**Q2** — materially impairs habitability or security before the next business
morning? YES → URGENT, NO → ROUTINE. Multi-issue messages classify at the
severity of the MOST severe issue, with every issue listed in reasoning.

### Where the rubric lives (do not paste it, learn to locate it)

- Human doc: `docs/02-product/severity-rubric-v1.md` — **v1.0, approved and
  frozen 2026-06-11**. The verbatim block is the fenced ```` ```text ````
  section headed `SEVERITY RUBRIC v1.0 — Stoop`.
- Code: `apps/api/app/agent/rubric.py` (`RUBRIC_V1`, `RUBRIC_VERSION = "1.0"`)
  — **byte-identical** to the doc's text block. `tests/test_rubric.py` pins a
  sha256 over `RUBRIC_V1` and cross-checks it against the doc, so drift in
  either direction fails CI.
- Prompt: `apps/api/app/agent/prompts/v1.py` embeds `RUBRIC_V1` by import
  (single source of truth, never a copy) into the `classify_severity` system
  prompt. The LIVE prompt package is `prompts/v2.py` (as of 2026-07-06, on
  merged via PR #177) — a templates-only bump that re-exports v1's
  system-prompt builders by construction, so the rubric embedding is
  byte-identical to what v1 shipped.

**The frozen doctrine:** the rubric is never edited in place, never
paraphrased, never "improved" inline. A behavior change is a *versioned
chain*: new doc (`severity-rubric-v2.md`) → new `rubric_v2.py` → new
`prompts/v{n+1}.py` (v3 next — v2 is already taken by the 2026-07-06
templates-only bump) → **full eval run** → graph switched to the new version.
The prefilter version is pinned to the rubric version too (below). If you
find yourself editing `rubric.py`, `prompts/v1.py`, `prompts/v2.py`, or the
doc's text block directly, stop — you are breaking project never-break rule 4.

### The six founder-signed judgment calls (2026-06-11) — with their reasoning

These were explicitly reviewed and approved; they are product positions, not
implementation details. Source: `docs/02-product/severity-rubric-v1.md`
"Judgment calls" section.

1. **Fire / medical / crime → 911 first.** The tenant is always told to call
   911 *before* anything else; then Stoop treats it as EMERGENCY. Reasoning:
   Stoop is not an emergency service and must never position itself as one —
   it must never sit between a person in danger and 911.
2. **The −10 °C no-heat line.** No heat is EMERGENCY when the outdoor
   temperature (current **or forecast overnight low**) is ≤ −10 °C; URGENT
   above that. Reasoning: Toronto's Property Standards bylaw minimum (21 °C
   indoors, Sept 15 – Jun 1) makes *any* in-season heat failure at least
   URGENT; the −10 °C outdoor line marks where it becomes dangerous rather
   than merely unlawful/uncomfortable. Heating season is stored per-property
   so other municipalities can differ.
3. **Vulnerable-occupant modifier bumps one level** — infant, elderly person,
   or someone dependent on powered medical equipment raises heat / power /
   water failures ONE level (e.g. no heat at −2 °C, normally URGENT, becomes
   EMERGENCY). Applies to those three failure classes only. The signal comes
   from the tenant record *or* from the message itself ("we have the baby
   this week") — the model uses both.
4. **When uncertain, escalate one level. Never round down.** The explicit
   product bias: a false alarm costs the landlord minutes; a missed emergency
   costs a flooded building. This "bias rule" is quoted inside the rubric
   itself and recurs everywhere (prefilter guard design, weather input
   design).
5. **Access control is absolute.** The agent never shares, confirms, or
   resets codes/keys/lockbox info and never authorizes entry — *regardless of
   the story given*. Reasoning: the agent cannot verify identity over SMS,
   and social engineering works precisely by supplying a plausible story; so
   no judgment is permitted at all. No exceptions.
6. **Rent, eviction, legal = landlord-only topics.** The agent acknowledges
   neutrally, never engages substance, flags for the landlord; classified
   ROUTINE unless paired with a maintenance issue. Reasoning: liability — see
   the Ontario section next.

Two more load-bearing rubric behaviors worth knowing cold: a **single
intermittent smoke-detector chirp is ROUTINE, but a CONTINUOUS alarm is
EMERGENCY** (this one sentence drives most of the prefilter's guard/veto
complexity), and **URGENT never auto-sends regardless of trust level**.

## Ontario landlord–tenant context (for a non-Canadian reader)

**The LTB** is Ontario's Landlord and Tenant Board — the provincial tribunal
that adjudicates residential tenancy disputes under Ontario's Residential
Tenancies Act (rent, evictions, maintenance obligations). Think of it as a
specialized court for tenancies; landlords and tenants file forms (the docs
mention N4/N5 eviction-notice forms) and disputes are decided on the record.

**Why Stoop never takes legal or rent positions:** everything the agent texts
a tenant is a written record made *on the landlord's behalf* — in an LTB
dispute it is evidence. A wrong sentence about rent, withholding, abatement,
or eviction could bind or prejudice the landlord. This is why:
- `audit_log` is append-only and explicitly described as "the
  dispute-resolution artifact (Ontario LTB) and the liability shield"
  (`docs/03-engineering/architecture.md`);
- the `legal_rent_ltb` refusal flag exists (below);
- project rule 8 additionally bans legal/LTB mentions in *marketing* copy —
  the product does the safe thing AND never advertises legal competence.
The paid tier even sells an "LTB pack" audit-trail export
(`docs/01-strategy/business-model.md`) — the same record-keeping discipline,
productized.

**Heating-season context behind the heat lines:** Toronto Property Standards
requires 21 °C indoors from Sept 15 to Jun 1. So during heating season a heat
failure is a bylaw violation from minute one — at least URGENT — and the
−10 °C outdoor threshold (judgment call 2) is where it escalates to
EMERGENCY. The rubric needs *outdoor* temperature data to apply this, which
is why the weather integration exists (section below).

**CASL and A2P 10DLC, in two paragraphs.** CASL is Canada's Anti-Spam
Legislation: commercial electronic messages (SMS included) require consent,
sender identification, and an opt-out mechanism. Stoop's tenant onboarding
message therefore includes identification and opt-out language
(`docs/03-engineering/architecture.md` §risk/compliance). A2P 10DLC is the
US carriers' registration regime for Application-to-Person traffic sent over
ordinary 10-digit long-code numbers: businesses must register their brand and
campaign with the carriers via their provider (Twilio here), or their traffic
gets silently carrier-filtered — messages "send" but never arrive.

Why this matters to Stoop specifically: the entire product is SMS, and
architecture.md is blunt that "unregistered traffic gets carrier-filtered
exactly when growth starts" — registration is "a milestone-1 task, not an
afterthought". Filing A2P/CASL paperwork is a **human-owned** task
(`apps/api/CLAUDE.md` "Things humans must do"); agents must never attempt it.
Status: registration is still pending, blocked on the founder (as of
2026-07-05; session-verified, no repo artifact). Do not build or promise
outbound-SMS volume features that assume registration is done.

## The five refusal flags and the deferral architecture

Canonical flag names (the `RefusalFlag` enum in
`apps/api/app/agent/schemas.py`; same keys in `REFUSAL_TEMPLATES` in
`app/agent/prompts/v2.py` — the LIVE template source as of 2026-07-06;
`prompts/v1.py` is frozen history with identical keys — verified 1:1):

| Flag | Protects against |
|---|---|
| `access_codes` | Social engineering into a unit: codes, keys, lockbox info, or authorizing entry for any third party. Absolute — no story is verifiable over SMS (judgment call 5). |
| `legal_rent_ltb` | Legal liability: rent amounts/increases/withholding, eviction, LTB proceedings. Agent text is LTB evidence; only the landlord may take positions. |
| `cost_compensation` | Unauthorized financial commitments: repair-cost promises, compensation, rent abatement — anything that binds the landlord's wallet. |
| `other_tenants` | Third-party privacy: never discuss another tenant's personal information. |
| `impersonation` | Consent forgery: entry notices, lease changes, formal consents must come from the landlord personally, not an AI writing in their voice. |

A refusal flag never suppresses severity: EMERGENCY is surfaced even when a
flag is set, and the non-refused remainder of the message is classified
normally (rubric REFUSAL TOPICS section; enforced in the classify prompt).

**The deferral architecture (one paragraph):** the model is *never* asked to
write, quote, or paraphrase refusal policy — it writes only a short
acknowledgment of the tenant's message, and the CODE appends the canned
`REFUSAL_TEMPLATES` text verbatim for every flag
(`_append_deferrals` in `apps/api/app/agent/nodes/draft_response.py`,
importing from `prompts.v2` — the v2 bump rewrote `legal_rent_ltb` and
`impersonation` plain-language, plained `access_codes` and
`cost_compensation`, and kept `other_tenants` byte-identical to v1
(commit `11564c8`, amended pre-merge by `31bd498`, which dropped "soon" —
eval gate 8 hard-failed f1 on that one relative-time word; the
`legal_rent_ltb` follow-up sentence now carries no time word at all);
quoting v1's template wording as "what the tenant receives" is now wrong);
`_strip_mandated_templates` scrubs any template text from the model's own ack
before guard-checking, as defense-in-depth. This separation exists because
the earlier design — model weaves the deferral language itself — failed two
ways: the model's paraphrase of a refused topic ("about the rent
discount…") tripped the compensation guard on an otherwise-safe reply, and
when the ack was told to mention the hand-off it duplicated what the appended
template already said, producing a reply that read as two texts glued
together. Hence the current instruction set: the ack must NOT state the
hand-off, NOT promise follow-up, NOT sign off, NOT touch the refused topic —
the appended template carries all of that. Standing ruling (2026-07-05,
recorded in the `draft_response.py` module docstring): the model
**acknowledges only; it never paraphrases policy**. Hard guards
(compensation-commitment, code/PIN/key-location, legal-position) then check
the model's own ack; one violation → one regeneration; a second →
`_GENERIC_SAFE_FALLBACK` — and the deferral templates are appended
unconditionally in every one of those outcomes.

## SMS-medium constraints

Tenants interact with Stoop through **SMS only** — no app, no portal, no
links they must tap. Binding rules: `docs/02-product/plain-language-rules.md`
(the founder principle: design for a reader who is panicked, half-asleep, on
a cracked phone, or reading English as a third language).

1. **Grade-5 reading level.** Short words, sentences ≤ 15 words, active
   voice. No jargon, no idioms ("shut-off valve" gets a location).
2. **One instruction per message-beat.** Emergency instructions are a
   numbered list, most important first, max three steps — nobody executes
   step 4 during a flood.
3. **ONE question, hard cap.** Plain-language rule 6 says clarifying
   questions never stack; the draft node enforces it literally — "at most ONE
   question mark in the whole reply" (`draft_response.py` drafting rules;
   added after an eval draft asked two questions at once).
4. **The ~300-char budget.** Routine replies ≤ 2 SMS segments ≈ 300 chars
   (`_LENGTH_BUDGET_CHARS = 300` in `draft_response.py`). The model is told
   its available budget up front (minus the length of any deferral that will
   be appended — the longest live template is 164 chars as of 2026-07-06,
   prompts v2; v1's longest was 223); an over-budget ack gets
   ONE regeneration. Refusal-flag drafts are exempt from the length check:
   never omitting the mandated deferral outweighs segment count.
5. **Truncation is forbidden.** If a guard-clean draft is still over budget
   after the one retry, the code NEVER cuts the text — a mechanical cut can
   amputate a safety step or deferral mid-sentence. The long draft is kept
   exactly as generated and `state["length_over_budget"] = True` flags it as
   a landlord-review signal ("you can shorten this before it sends").
6. **Segments/cost intuition:** a single GSM-7 SMS is 160 chars; concatenated
   messages carry ~153 chars per segment, and every segment is billed
   separately. ~300 chars ≈ 2 segments is the price of one thorough reply;
   walls of text are simultaneously worse UX and higher cost — "a failure
   mode, not thoroughness."
7. **Never correct the tenant.** Typos, ALL-CAPS, slang, fragments are valid
   input; the agent understands them and never comments or mirrors them.
   Tone under stress: calm, warm, certain — never scolding or legalistic.
   Concrete over relative ("Thursday between 8 and 11", never "soon") —
   enforced even inside refusal templates: a single "soon" hard-failed eval
   f1 at gate 8 and was excised in `31bd498` (2026-07-06).

## Tier-0 prefilter theory — why a regex layer runs UNDER the LLM

Principle (`docs/02-product/emergency-prefilter.md`): *Stoop is a safety
system with an AI in it, not an AI product.* The emergency path must work
when the LLM is slow, down, or wrong. Tier 0 is that floor: pure regex over a
normalized string, no I/O, no network, sub-millisecond, running
**synchronously in the Twilio webhook handler before the agent graph**.
Implementation: `apps/api/app/agent/prefilter.py`,
`check(text) -> PrefilterResult` (`hard_hit`, `categories`,
`soft_annotations`, `guards` — snapshotted into `messages.prefilter` jsonb).

**Composition with the classifier:** a HARD hit fires the emergency protocol
immediately; the agent still runs afterward (context, follow-up drafts). The
agent may escalate a Tier-0 *miss*; it may **never de-escalate a Tier-0
fire** — if the LLM disagrees, the call already happened, and that is the
bias rule working as designed. Disagreements are logged and reviewed as guard
candidates or eval cases.

**Trigger / guard / veto semantics** (the three-layer vocabulary):

- **HARD triggers** — five categories: `fire`, `gas_co`, `water`,
  `security`, `person`. Two shapes: SIMPLE (a match is a hit) and PROXIMITY
  (an anchor keyword plus a nearby word within a character window, e.g.
  "gas" near "smell(ed)"). Hits are keyed on the **anchor token span**, not
  the whole window.
- **Guards** — narrow, deliberately small exclusions for known false
  positives (smoke/fire/CO-detector *battery* mentions, "fire drill", "fire
  alarm test", and the fixture compound nouns fire escape / extinguisher /
  pit / hydrant). A guard suppresses only anchor tokens that fall inside its
  CORE phrase span — never anything downstream. **When a guard and an
  independent trigger both match, the trigger wins.**
- **Vetoes** — two never-suppressible mechanisms sit above the guards:
  (a) continuous-alarm triggers are `suppressible=False`: a smoke/fire/CO
  alarm that is blaring / nonstop / going off / won't stop is EMERGENCY per
  the rubric **even when the message also mentions a battery** — no battery
  guard may silence it; and (b) the fixture guards carry a `refuse_if`
  whole-message hazard veto (`flames|smoke|burning|on fire` anywhere in the
  message deactivates the guard entirely, so "flames shooting out near the
  fire escape" fires).
- The continuous-alarm phrasing list is ONE shared alternation
  (`_CONTINUOUS_ALARM_PHRASES`) reused by all six continuous-alarm triggers
  and all battery-guard vetoes. Three consecutive safety-review rounds each
  found a missed synonym in a hand-copied variant of that list — **never
  inline a copy of it.**
- **SOFT annotations** (`no_heat`, `freezing`, `sparks`, `leak`,
  `locked_out`) never fire the protocol alone; they lower the degraded-mode
  bar and are surfaced to the classifier as hints.
- Normalization handles adversarially mundane input: NFKD unicode folding
  ("éverywhere" → "everywhere"), unicode-dash → ASCII "-", and a "9-1-1" →
  "911" collapse that runs before punctuation stripping.

**The monotonic-additive maintenance discipline:** changes to the prefilter
are additive only — new triggers, completed inflection sets, tightened-scope
guards — with **zero HARD→silent flips** and a regression test class in
`apps/api/tests/test_prefilter.py` for every change. The canonical incident:
the eval message "the kitchen has smelled like gas" did not fire because the
word list had "smell"/"smells" but never past-tense "smelled" —
hand-copied-alternation drift. The fix class is *tense completion*: complete
the base/3rd-person/past/progressive forms of verbs already present, never
introduce new vocabulary in the same change. (Full incident history:
`stoop-failure-archaeology`.)

**Version pinning:** `PREFILTER_VERSION = "1.0"` is pinned to rubric v1.0 —
the HARD list is *generated from* the rubric's EMERGENCY section. A rubric
bump means a new prefilter module, never in-place pattern edits.

**Degraded mode** (LLM down/slow — `emergency-prefilter.md`): classification
budget 20 s end-to-end, one retry. On failure: HARD hit already handled
without the LLM; SOFT annotation present → escalate blind (landlord gets a
"needs your eyes" notification, tenant gets the holding ack); no keywords →
holding ack + re-classification at 1/5/15 min, then needs-your-eyes anyway.
The invariant: **no tenant message ever sits unacknowledged and invisible
because an API was down.**

## The trust ladder (UNBUILT — issue #60) and why approval-first is the default

Concept: `trust_metrics` (schema: `docs/03-engineering/schema-v1.md`) tracks,
per `(property, severity)`, clean approvals / edited approvals / rejections
and a `consecutive_clean` graduation counter. Enough consecutive
approvals-without-edits unlock `autonomy_unlocked` — auto-send — **for
ROUTINE only** (`autonomy_unlocked` is "only ever true for routine in v1";
`drafts.auto_send` defaults false, "true only via trust ladder (#60)").
Unlocks are per-property, per-severity, always revocable
(`revoked_at`). Emergency and urgent never auto-send at any trust level.
"The trust ladder is data, not vibes" (`architecture.md` §5).

**Status (as of 2026-07-05): unbuilt.** The table is specified in schema-v1.md
and referenced by a schema comment, but no application code implements
graduation or auto-send (grep `trust_metrics` in `apps/api/app/` — only a
passing docstring mention in `schemas.py`). Treat any auto-send behavior you
are asked to build as gated on #60 and on project rule 3.

**Why approval-first is the default:** project never-break rule 3 — nothing
sends to a tenant or vendor without landlord approval, except emergency
safety instructions. The draft goes out in the *landlord's* voice and under
the landlord's legal exposure (LTB section above), so consent must be earned
per-property and per-severity from observed behavior, not assumed. Early
edits and rejections are also the training signal that makes drafts better —
skipping the approval phase would discard exactly the data the ladder
graduates on.

## Weather integration rationale

The rubric cannot apply judgment call 2 without outdoor temperature: "no heat
at ≤ −10 °C (current or forecast overnight low)" and "no A/C during an
official heat warning" both silently fail with no weather input
(severity-rubric-v1.md, implementation notes). Hence
`apps/api/app/integrations/weather.py`:

- **Open-Meteo, keyless.** Chosen precisely because it needs no credential —
  credential provisioning is a human-owned task, and this ships without one.
- **Degradation-safe by contract.** `get_weather_snapshot(lat, lon)` never
  raises, never blocks past a 3 s timeout, returns `None` on any failure or
  when the property has no coordinates. Callers proceed with classification
  regardless — the rubric's own bias rule covers a missing reading (uncertain
  → escalate).
- **Overnight low = min of today's and tomorrow's daily minima**
  (`forecast_days=2`). Using only today's minimum has a real bug shape: run
  in the evening, "today's low" is this *morning's* (already-past, warmer)
  reading exactly when tonight's cold is still ahead. Taking the two-day min
  can only err colder — the bias rule applied to the *input*.
- **Heat warning is a documented approximation:** `True` when today's
  forecast high ≥ 31.0 °C (Environment Canada's common Southern-Ontario
  daytime threshold). Open-Meteo's free endpoint has no government-alert
  feed; this is flagged in the module as a candidate for a real alerts
  integration, not silently authoritative.
- 30-minute in-process TTL cache keyed on coordinates rounded to 2 decimals
  (~1.1 km); rounded coordinates are the only location data ever logged
  (privacy rule 5 — never phone numbers, addresses, or message bodies).

## Supabase / Supavisor platform pack

This is the one home for Supabase platform theory. Operating procedures live
in `stoop-run-and-operate`; incident narratives in `stoop-failure-archaeology`.
Supavisor is Supabase's connection pooler: port 5432 = session pooling,
port 6543 = transaction pooling (backend connections multiplexed per
transaction — no stable session for a client to assume).

**1. The three asyncpg knobs for the transaction pooler (6543).** asyncpg's
prepared-statement machinery assumes a stable session; under transaction
pooling you get intermittent `DuplicatePreparedStatementError`. SQLAlchemy's
*documented* two-knob recipe (dialect-level `prepared_statement_cache_size=0`
plus a UUID statement-name function) is **insufficient**, because
`pool_pre_ping`'s ping bypasses the dialect and uses asyncpg's own cache — a
third, asyncpg-level knob `statement_cache_size=0` is required. All three
live in `_ASYNCPG_POOLER_CONNECT_ARGS`, defined in BOTH
`apps/api/app/db/session.py` and `apps/api/migrations/env.py`. Never remove
any of the three. (Live probe evidence: 18/100 request failures with two
knobs → 0/100 with three — session-verified 2026-07-05; the knobs and
reasoning are in the code comments.)

**2. The psycopg3 (LangGraph checkpointer) equivalents.** The checkpointer
pool (`apps/api/app/agent/checkpointer.py`) needs `prepare_threshold=None` —
**not 0: 0 means prepare-everything**, the opposite — plus `autocommit=True`
(its `setup()` runs `CREATE INDEX CONCURRENTLY`, which cannot run in a
transaction block) and `search_path` pinned twice (connection `options` AND a
`configure` callback).

**3. `postgres` on Supabase is NOT a superuser.** It (and `service_role`)
has `rolbypassrls = TRUE`, but privileged operations that local Docker
Postgres happily allows can fail live (`must be able to SET ROLE …`).
Consequence and standing rule: **any migration touching roles, grants, or
RLS gets a live Supabase dry-run before merge** — local Docker runs as a
bootstrap superuser and is blind to privilege bugs. (Facts live-probed
2026-07-04; recorded in `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py`'s
docstring.)

**4. The `pg_has_role` MEMBER trap (PG16+).**
`pg_has_role(current_user, 'newrole', 'MEMBER')` returns TRUE immediately
after `CREATE ROLE newrole` (implicit ADMIN OPTION for the creator), so it is
unusable as an idempotency or membership guard. Recorded in migration 0004's
docstring.

**5. `GRANT <role> TO CURRENT_USER` executed as `postgres` terminates the
connection** — reproduced on both pooler ports (session-verified 2026-07-04;
no repo artifact beyond the redesign). The surviving design: a
postgres-owned `SECURITY DEFINER` function with a pinned `search_path`, and
NO custom-role membership grants involving `postgres` anywhere (migration
0004).

**6. RLS is ENABLE, never FORCE.** `FORCE ROW LEVEL SECURITY` broke
first-login provisioning (the INSERT happens before any landlord identity
exists to satisfy a policy). Design: `ENABLE` only, with provisioning on the
admin engine (`get_admin_session`), whose callers are allowlist-tested
(`apps/api/tests/test_migrations_0005.py::test_get_admin_session_referenced_only_by_allowlisted_files`).
Do not "upgrade" ENABLE to FORCE without re-solving provisioning. Full
reasoning: migration `0005_app_role_and_rls.py` docstring.

**7. Never trust the URL's username — compare server-side identity.** The
boot self-check `verify_request_engine_role_separation()`
(`app/db/session.py`, called from `app/main.py` lifespan) compares the
SERVER-reported `current_user` of both engines instead of parsing connection
URLs; part of the reason is that Supavisor's login usernames carry a project
suffix, so the URL string and the server's answer differ (suffix detail
session-verified 2026-07-05; the server-side-comparison design is in the
`session.py` docstring).

**8. Live-project snapshot (as of 2026-07-05; session-verified, no repo
artifact except where noted):** project ref `kytqtdqmzwyhiwkafcbh`, region
`ca-central-1` (Canadian data residency is a repo requirement —
`architecture.md`), PG 17.6, migrations applied through head `0008` (the
migration files `0001`–`0008` are in `apps/api/migrations/versions/`). The
`APP_DATABASE_URL` cutover is NOT done: `app_role` is still `NOLOGIN`; an
operator must `ALTER ROLE app_role LOGIN PASSWORD …` and set the env var
(procedure: `stoop-run-and-operate`). Never connect to the live database
(`*.pooler.supabase.com`) for casual verification — use local Docker.

## Provenance and maintenance

Drift-prone claims and one-line re-verification commands (run from repo
root unless noted):

| Claim | Re-verify with |
|---|---|
| Rubric v1.0 frozen, byte-identical embedding + pinned sha256 | `cd apps/api && uv run pytest tests/test_rubric.py -m unit -q` |
| Prefilter v1.0 pinned to rubric v1.0 | `grep -n 'PREFILTER_VERSION\|RUBRIC_VERSION' apps/api/app/agent/prefilter.py apps/api/app/agent/rubric.py` |
| Five refusal flag names / templates 1:1 (+ longest length) | `cd apps/api && uv run python -c "from app.agent.prompts.v2 import REFUSAL_TEMPLATES as T; print(sorted(T), max(len(v) for v in T.values()))"` (on `main` since PR #177) |
| Code-appends deferral architecture (`_append_deferrals`, `_strip_mandated_templates`) | `grep -n '_append_deferrals\|_strip_mandated_templates' apps/api/app/agent/nodes/draft_response.py` |
| 300-char budget, truncation forbidden, one-question hard cap | `grep -n '_LENGTH_BUDGET_CHARS\|TRUNCATION IS FORBIDDEN\|ONE question' apps/api/app/agent/nodes/draft_response.py` |
| Continuous-alarm shared list + `refuse_if` vetoes | `grep -n '_CONTINUOUS_ALARM_PHRASES\|refuse_if\|suppressible=False' apps/api/app/agent/prefilter.py` |
| Trust ladder still unbuilt (#60) | `grep -rn trust_metrics apps/api/app/` (only a docstring mention ⇒ still unbuilt) |
| Weather constants (3 s timeout, 30 min TTL, 31.0 °C, 2-day min) | `grep -n '_TIMEOUT_SECONDS\|_CACHE_TTL_SECONDS\|_HEAT_WARNING_MAX_TEMP_C\|forecast_days' apps/api/app/integrations/weather.py` |
| Three asyncpg pooler knobs present in both places | `grep -n 'statement_cache_size' apps/api/app/db/session.py apps/api/migrations/env.py` |
| Checkpointer `prepare_threshold=None` + autocommit | `grep -n 'prepare_threshold\|autocommit' apps/api/app/agent/checkpointer.py` |
| ENABLE-not-FORCE RLS + admin-session allowlist test | `grep -n 'test_get_admin_session_referenced_only_by_allowlisted_files' apps/api/tests/test_migrations_0005.py` |
| Migration head (0008 as of 2026-07-05) | `ls apps/api/migrations/versions/` |
| Pricing/copy rules (free Emergency Line, "early access" wording) | `grep -n 'Emergency Line\|early access' CLAUDE.md` |
| A2P/CASL status (pending, human-owned) | no repo artifact — ask the founder; the *obligation* is in `apps/api/CLAUDE.md` "Things humans must do" |
| Live project ref / PG version / `app_role` LOGIN state | no repo artifact — Supabase dashboard (human) or `stoop-run-and-operate` |

Maintenance rule: this file describes doctrine and platform physics, which
move slowly — but if the rubric ever goes to v2, the prefilter to v2, a
prompt version bump lands (one has: prompts v2, templates-only,
2026-07-06 — reflected above), or #60 ships, update the affected section in
the same PR and re-date the "(as of)" stamps. Never let this file contradict `severity-rubric-v1.md` or either
`CLAUDE.md`; those win.
