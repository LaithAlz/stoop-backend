# Stoop — System Architecture

> **Status:** Adopted 2026-06-11. Supersedes the implicit architecture in the original 9-epic roadmap.
> **Decision summary:** Python/FastAPI backend with LangGraph (Option B), two vendors cut (Clerk → Supabase Auth, Inngest → deferred), 9 phases compressed to 3 milestones. Rationale in [Decision log](#decision-log).

---

## 1. The product loop (what the architecture must serve)

```
tenant texts a Twilio number
        │
        ▼
Stoop classifies severity ──────────► EMERGENCY: call the landlord NOW,
  (emergency / urgent / routine)      text tenant safety steps
        │
        ▼
drafts a reply in the landlord's voice
        │
        ▼
landlord approves / edits in the dashboard  ◄── trust ladder: enough clean
        │                                       approvals → routine replies
        ▼                                       auto-send
reply goes out via SMS · everything logged to the audit trail
```

Every architectural decision below is judged against this loop. Anything that
doesn't serve it before milestone 3 is cut or deferred.

---

## 2. System diagram

```
                                ┌─────────────────────────────┐
                                │   Tenant (SMS, no app)      │
                                └──────────┬──────────────────┘
                                           │ SMS
                                ┌──────────▼──────────────────┐
                                │   Twilio (per-property #)   │
                                └──────────┬──────────────────┘
                                           │ webhook (signed)
┌──────────────────────┐        ┌──────────▼──────────────────────────────┐
│  Landlord dashboard  │  HTTPS │   apps/api — FastAPI on Fly.io (yyz)    │
│  apps/web            ├───────►│                                         │
│  TanStack Start      │  JWT   │  /webhooks/twilio   inbound messages    │
│  Cloudflare Workers  │        │  /v1/*              authed REST API     │
└──────────┬───────────┘        │  agent/             LangGraph graph     │
           │                    └───┬───────────┬───────────┬─────────────┘
           │ auth (JWT)             │           │           │
┌──────────▼───────────┐            │           │           │
│  Supabase Auth       │            │  ┌────────▼────────┐  │ traces
│  (GoTrue, JWKS)      │            │  │  Anthropic API  │  │
└──────────┬───────────┘            │  │  (Claude)       │  │
           │ auth.users             │  └─────────────────┘  │
┌──────────▼─────────────────────┐  │                ┌──────▼──────────┐
│  Supabase Postgres             │◄─┘                │  LangSmith      │
│  app schema + RLS              │                   │  (traces+evals) │
│  LangGraph checkpoints         │                   └─────────────────┘
│  audit_log (append-only)       │     ┌─────────────────┐
└────────────────────────────────┘     │  Sentry         │ errors (api+web)
                                       └─────────────────┘
```

Two deployables. One database. Seven external services (Supabase, Twilio,
Anthropic, LangSmith, Sentry, PostHog, Plausible). Stripe joins with
billing (Train 2).

---

## 3. Stack and rationale

| Layer | Choice | Why (and why not the alternative) |
|---|---|---|
| Backend | Python 3.12, FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic | Founder is fluent; best ecosystem for the agent layer. All-TS on Cloudflare was considered and rejected: founder has no Cloudflare experience and wants to learn LangGraph (Python-first). |
| Agent | LangGraph + Anthropic SDK, `AsyncPostgresSaver` checkpointer | Deliberate learning goal, not just resume-driven: checkpointed state machines, interrupts (approval gates), and eval workflows are the actual curriculum. `classify_severity` calls the Anthropic SDK directly (not a wrapper) for full control of the rubric prompt. |
| Observability (LLM) | LangSmith from the first node | Tracing + eval datasets. Production misclassifications feed the eval corpus (see §9). |
| Database | Supabase Postgres, RLS at milestone 2 | One Postgres for app state, agent checkpoints, and audit log. RLS is the multi-tenant isolation mechanism, enforced by `app.current_landlord_id` session variable. |
| Auth | **Supabase Auth** (replaces Clerk) | Already paying for Supabase; same JWT/JWKS verification pattern; one fewer vendor, dashboard, and bill. See ADR-1. |
| Jobs/queue | **None at v1** (replaces Inngest) | Twilio webhooks + FastAPI background tasks cover the loop. A durable queue is added on trigger, not on faith. See ADR-2 and §11. |
| API hosting | Fly.io, `yyz` region | As originally specced. Toronto region matches the Ontario-landlord market and keeps DB latency to Supabase low (host Supabase in `ca-central-1`). |
| Frontend | TanStack Start + shadcn/ui on Cloudflare Workers | Already built and deployed. Cloudflare here is invisible plumbing (`bun run deploy`); not a learning burden. Design direction: "Brownstone" (`docs/mockups/04`, `05`). |
| SMS | Twilio | Industry default. **A2P 10DLC / CASL registration is a milestone-1 task, not an afterthought** — unregistered traffic gets carrier-filtered exactly when growth starts. |
| Errors | Sentry (api + web) | As specced. |
| Product analytics + flags | **PostHog** (free tier) | Four tools, one vendor: behavioral analytics, feature flags, experiments, surveys. Session replay **off** (the dashboard displays tenant PII). Identify by landlord uuid only. See ADR-5. |
| Marketing analytics | **Plausible** | Anonymous, no consent banner, custom events on waitlist submit; pairs with the D1 `source` column for channel attribution. |
| Payments | Stripe, milestone 3 only | Nothing billing-related is built before there's something worth paying for. |

---

## 4. Backend (`apps/api`)

```
app/
  main.py               app factory, /healthz, /readyz
  settings.py           pydantic-settings, all config from env
  deps.py               require_user (JWT) → require_landlord (DB row + RLS var)
  db/                   engine, session, Alembic migrations
  models/               SQLAlchemy models (landlords, properties, tenants,
                        conversations, messages, notifications, trust_metrics,
                        audit_log, push_tokens)
  routers/
    me.py               GET /v1/me (gate for milestone 1 deploy)
    conversations.py    list/detail, approve/edit draft
    webhooks/twilio.py  inbound SMS (signature-verified)
  agent/
    graph.py            StateGraph assembly + AsyncPostgresSaver
    state.py            AgentState TypedDict
    nodes/              identify_property, load_context, classify_intent,
                        classify_severity, draft_response, emergency_protocol
    prompts/v1.py       frozen prompt versions (v1, v2, …)
    rubric.py           severity rubric — embedded verbatim in every
                        classify_severity call
    tools.py            Pydantic tool schemas → JSON schema
  integrations/
    supabase_auth.py    JWKS fetch/cache + JWT verification
    twilio.py           send SMS, validate webhook signatures
    anthropic.py        client factory, token/cost accounting helper
  evals/
    scenarios/          YAML eval cases (emergency/urgent/routine/refusal/…)
    runner.py           pytest -m eval, runs against the real API
```

**Request auth flow:** `Authorization: Bearer <supabase JWT>` → verify via
JWKS (asymmetric, cached 24h) → `require_landlord` upserts/loads the
`landlords` row by `auth_user_id` → sets `app.current_landlord_id` on the DB
session (RLS) → handler runs. Stateless; no sessions server-side.

**Twilio inbound flow:**

1. `POST /webhooks/twilio` — signature verified, payload persisted to
   `messages` **before any processing** (never lose a tenant message), 200
   returned immediately.
2. Background task invokes the LangGraph graph with the conversation thread id.
3. Graph runs: identify property (by Twilio number) → load context (tenant,
   property notes, house rules, conversation history) → classify intent →
   classify severity → branch:
   - **emergency** → `emergency_protocol` node: voice call to landlord
     (Twilio), immediate safety SMS to tenant, audit entries. No approval gate
     — safety messages send instantly.
   - **urgent / routine** → `draft_response` → **interrupt** (LangGraph
     checkpoint) → draft surfaces in the dashboard approval queue.
4. Landlord approves (or edits) → graph resumes from checkpoint → SMS sent →
   `trust_metrics` updated → audit entries written.
5. The same approval queue carries **vendor messages** (v1, issue #115):
   "Ask your plumber" drafts an SMS to the landlord's own tradesperson,
   approval-first, with replies threading onto the case. Stoop relays —
   the vendor never sees the tenant's number.

The approve action implements **undo-with-delay** (5 s) before the actual
Twilio send, matching the dashboard design — sending SMS to a tenant is
irreversible, so the irreversibility is buffered in UX, not pretended away.

---

## 5. Agent design

```
            ┌────────────────┐
 inbound ──►│ identify_      │  Twilio # → property; sender # → tenant
            │ property       │
            └──────┬─────────┘
            ┌──────▼─────────┐
            │ load_context   │  tenant, unit, house rules, voice profile,
            └──────┬─────────┘  open conversations, recent history
            ┌──────▼─────────┐
            │ classify_intent│  maintenance / admin / question / other
            └──────┬─────────┘
            ┌──────▼─────────┐
            │ classify_      │  rubric embedded verbatim; Anthropic SDK
            │ severity       │  direct; returns severity + cited reasons
            └──────┬─────────┘
        emergency? │
       ┌───────────┴───────────┐
┌──────▼─────────┐    ┌────────▼───────┐
│ emergency_     │    │ draft_response │  landlord voice profile
│ protocol       │    └────────┬───────┘
│ (no approval   │    ┌────────▼───────┐
│  gate)         │    │ INTERRUPT      │  checkpoint; waits for approval
└────────────────┘    └────────┬───────┘
                      ┌────────▼───────┐
                      │ send + log +   │
                      │ trust update   │
                      └────────────────┘
```

Rules that don't bend:

- **Severity rubric is versioned and embedded verbatim** in every
  `classify_severity` prompt. No paraphrasing, no drift.
- **Every node appends to `reasoning_log`** in `AgentState`. This powers the
  "WHY URGENT — …" trace shown on the approval card (a product feature, not
  just debugging) and makes LangSmith traces human-readable.
- **Prompts are frozen files** (`prompts/v1.py`). A prompt change is a new
  version + eval run, never an in-place edit.
- **The trust ladder is data, not vibes:** `trust_metrics` tracks approvals
  without edits per (landlord, property, severity). Auto-send unlocks
  per-property, per-severity, and is always revocable. Emergency and urgent
  never auto-send in v1 regardless of trust level.

---

## 6. Data model (overview)

Full schema lands in milestone 1–2 migrations; shape:

- `landlords` — keyed by `auth_user_id` (Supabase `auth.users.id`), voice
  profile, timezone, subscription fields.
- `properties` — address, Twilio number, house rules text, quiet hours.
- `tenants` — phone number, unit, property FK.
- `conversations` — thread per (tenant, topic); holds LangGraph thread id,
  status (open / awaiting_approval / resolved), severity.
- `messages` — **append-only**: direction, body, media refs, Twilio SIDs,
  classification snapshot. Never updated, never deleted.
- `trust_metrics` — rolling approval/edit counts per (landlord, property,
  severity).
- `audit_log` — **append-only**: every classification, draft, approval, edit,
  send, emergency call, with actor (`agent` / `landlord`) and timestamps.
  This is the LTB-dispute artifact and the liability shield. Treat as
  load-bearing product surface.
- `notifications`, `push_tokens` — emergency call/notify bookkeeping.
- LangGraph checkpoint tables — managed by `AsyncPostgresSaver.setup()`.

Conventions (from the original spec, kept): `uuid` PKs, `timestamptz`
everywhere, explicit FK cascade behavior, RLS on every multi-tenant table
(milestone 2), service-role connection separated from request connections.

---

## 7. Auth (Supabase) — what changed from Clerk

| Concern | Clerk (old plan) | Supabase Auth (adopted) |
|---|---|---|
| JWT verification | JWKS, RS256 | JWKS, asymmetric signing keys (enable in project settings — do **not** use the legacy shared HS256 secret) |
| Issuer | `https://<app>.clerk.accounts.dev` | `https://<ref>.supabase.co/auth/v1` |
| User id claim | `sub` (Clerk user id) | `sub` (UUID in `auth.users`) |
| Providers | Email+password, Apple, Google | Same, configured in Supabase dashboard |
| Lifecycle sync | Svix webhooks (#15) | Postgres trigger on `auth.users` → `landlords` (same DB — no webhook needed); lazy upsert in `/v1/me` stays as the safety net |
| Cost | Separate bill | Included in existing Supabase project |

The `require_user` dependency is the same pattern the Clerk spec described:
extract bearer token → read `kid` → match against cached JWKS → verify
signature/exp/iss → typed identity object. Issues 004/010/011/015 are
rewritten accordingly.

---

## 8. The severity contract (product invariants)

These are architecture-level guarantees, enforced in code and tested in evals:

1. **Emergency → landlord's phone rings.** Median target < 60 s from inbound
   SMS. No approval gate on tenant safety instructions.
2. **Nothing non-emergency makes noise at night.** Urgent drafts wait in the
   morning queue; routine waits or auto-sends (trust-gated).
3. **Nothing sends without approval** until that (property, severity) rung is
   explicitly unlocked by trust metrics.
4. **False-negative emergencies are the catastrophic failure mode.** The
   rubric, evals, and any prompt change are reviewed against this case first.
   When uncertain between urgent and emergency, the agent escalates.

---

## 9. Observability, evals, and the data moat

- **LangSmith tracing from the first node.** Each trace readable end-to-end
  via `reasoning_log`.
- **Eval suite is a v1 feature, not hardening.** Initial 10 scenarios
  (emergency / urgent / routine / refusal) run via `pytest -m eval` against
  the real Anthropic API. CI runs evals on any change touching
  `agent/prompts/` or `agent/rubric.py`.
- **Every production misclassification becomes an eval case.** This corpus is
  the startup's compounding asset: at 10 landlords a misclassification is an
  apology; at 1,000 it's a flooded unit and a churn story. The eval corpus is
  how classification quality scales with the customer base — and it cannot be
  shortcut by a competitor.
- **Cost metering from message one.** `integrations/anthropic.py` records
  tokens + model + cost per message into the message row. Unit economics
  ($19/door vs. LLM cost per door) must be a query, not a guess.
- **Sentry** for both apps; structured JSON logs with `request_id` on Fly.

### Analytics & feature flags (ADR-5)

Three layers, each owning different questions:

1. **Postgres owns business truth** — approval latency, edit rates,
   emergency acknowledgment time, retention, cost per door. These are
   domain queries on tables we already write (audit_log, trust_metrics,
   messages); never outsourced to a vendor.
2. **PostHog owns behavioral questions** (dashboard only): sessions,
   exits, drop-off, feature usage — plus **feature flags, experiments,
   and surveys**. v1 event spec: `session_start/end`,
   `onboarding_step_completed`, `queue_viewed`,
   `draft_approved|edited|rejected` (with UI timing), `undo_used`,
   `case_opened_from_push`, `settings_changed`. Server-side SDK (Python)
   evaluates flags where decisions live (e.g. pricing cohort — prices are
   chosen server-side, never client-supplied).
3. **Plausible owns the marketing site**: pageviews, referrers,
   `waitlist_submitted` events.

Hard rules:
- **PII:** identify users by landlord uuid only — never email, name, or
  phone. No message bodies, tenant names, or phone numbers in event
  properties. Session replay stays off until there is a reviewed,
  mask-everything configuration and a reason.
- **Flags gate product features, never safety behavior.** The emergency
  path, the rubric, and approval-first sending are governed by code,
  trust-ladder data, and eval-gated releases. If PostHog is down,
  unreachable, or misconfigured, the severity contract must be unchanged
  — SDKs run with local evaluation and safe fallbacks.
- Events proxied through our own domain (adblocker signal loss).

---

## 10. Security, privacy, compliance

- **Tenant PII** (phone numbers, message contents) lives only in Postgres;
  never in logs, never in Sentry breadcrumbs, never in LLM traces beyond what
  classification requires. No JWTs in logs, ever.
- **Audit log is append-only** — enforced by revoking UPDATE/DELETE from app
  roles. It is the dispute-resolution artifact (Ontario LTB) and the answer
  to "what did the AI send on my behalf?"
- **SMS compliance:** A2P 10DLC (US) / CASL + carrier registration (Canada)
  filed during milestone 1. Tenant onboarding message includes identification
  and opt-out language.
- **RLS as the isolation mechanism** (milestone 2): every multi-tenant table;
  cross-tenant isolation tests are the milestone gate, not a checkbox.
- **Data residency:** Supabase project in `ca-central-1` — Canadian landlord/
  tenant data stays in-country; one less objection in sales conversations.
- Secrets in Fly/Cloudflare secret stores; `.env.example` documents every
  var; gitleaks in pre-commit (already specced).

---

## 11. Scaling plan — triggers, not faith

The stack above serves ~1,000 landlords (≈50k messages/day ≈ 0.5 rps) without
structural change. Scale work is added when a trigger fires, not on a roadmap:

| Trigger (measurable) | Action |
|---|---|
| Twilio webhook retries observed / background task losses | Add a durable queue (Inngest or Temporal) between webhook and graph — the seam is already clean (webhook persists, then enqueues) |
| p95 dashboard query > 300 ms or DB CPU sustained > 60% | Add indexes first; then Supabase compute upgrade; then read replica |
| > ~50 messages/min sustained | Second Fly machine (the API is stateless; this is a slider) |
| First property-management company asks | SOC 2 program; until then, the audit log + RLS tests are the security story |
| Eval corpus > ~200 cases / prompt iteration slows | Dedicated eval infra (LangSmith datasets + scheduled runs) |
| Multi-region | Realistically never for this product; revisit only if expanding beyond North America |

What must be right from day one because it cannot be retrofitted: the data
model (multi-tenancy, append-only audit), carrier registration, and the eval
discipline. Everything else is swappable.

---

## 12. Decision log

**ADR-1 — Supabase Auth replaces Clerk** (2026-06-11)
Both implement the same JWKS/JWT pattern the backend verifies. Supabase Auth
is already paid for, removes a vendor/dashboard/bill, and user-lifecycle sync
becomes a same-database trigger instead of signed webhooks. Cost: rewriting 4
issue specs; losing Clerk's nicer hosted UI (acceptable — the dashboard owns
sign-in UX). Revisit if: org/team features or enterprise SSO become required
(Clerk/WorkOS re-enter then).

**ADR-2 — Inngest deferred** (2026-06-11)
v1 load (single-digit rps ceiling) is served by Twilio webhooks + FastAPI
background tasks, with messages persisted before processing so a crashed task
loses work, not data. A durable queue is reintroduced at the trigger in §11.
Cost: a window where a crash mid-graph requires the conversation to be
re-poked (acceptable: LangGraph checkpoints make resumption cheap).

**ADR-3 — Python/LangGraph over all-TypeScript** (2026-06-11)
All-TS-on-Cloudflare would consolidate to one language and one platform, but:
founder has zero Cloudflare experience (new platform risk on the critical
path), is equally fluent in both languages (no fluency win), and explicitly
wants to learn LangGraph/LangSmith (motivation and skill compounding).
Two-deployable cost is accepted; the web app's Cloudflare hosting requires no
ongoing platform knowledge.

**ADR-5 — PostHog + Plausible for analytics; flags never gate safety** (2026-06-12)
Firebase/GA4 rejected (consent burden, adblocker signal loss ~30%, weak
product-analytics on web, ecosystem mismatch with Supabase/Fly/Cloudflare).
Mixpanel/Amplitude dominated by PostHog's free tier + flags/experiments/
surveys in one vendor. Session replay off — the dashboard renders tenant
PII. Marketing stays anonymous on Plausible. Boundary rule: feature flags
may gate rollouts, pricing cohorts, and experiments; they may never alter
the emergency path, rubric behavior, or approval requirements.

**ADR-4 — 9 phases compressed to 3 milestones** (2026-06-11)
Original phases optimized for completeness; milestones optimize for
time-to-first-paying-landlord. See `stoop-all-epics.md` (rewritten) for the
mapping. Mobile app, Inngest, and multi-region drop off the critical path
entirely.
