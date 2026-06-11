---
title: "roadmap: Stoop v1 — three milestones"
labels: ["roadmap"]
milestone: ""
---

# Stoop · Roadmap (v2)

> **Rewritten 2026-06-11.** Supersedes the 9-epic roadmap (in git history).
> Architecture and rationale live in [`architecture.md`](./architecture.md) —
> read that first. Changes: Clerk → Supabase Auth (ADR-1), Inngest cut
> (ADR-2), 9 phases → 3 milestones (ADR-4). Old epic numbers are mapped below
> so existing issues keep making sense.

Three milestones from zero to first paying landlord. Each has one gate — a
demo you can show, not a checklist feeling done. Everything not on this page
is deliberately off the critical path (see "Cut or deferred").

---

## Milestone 1 · Walking skeleton

**Absorbs:** old Epics 1 (backend foundation), 3 (agent skeleton), 4 (Twilio loop), minus everything multi-tenant.
**Duration target:** ~4–5 weeks part-time.
**Gate:** *A real tenant texts a real Twilio number. Stoop classifies it, drafts a reply in your voice, you approve it on the deployed dashboard, the tenant gets the SMS. The whole run is visible as one LangSmith trace, and all 10 eval scenarios pass.*

One landlord (you), your properties, production deployment. No billing, no
RLS, no strangers.

### In

- FastAPI app factory, health endpoints, settings, structured logging,
  Sentry, Docker, Fly.io (`yyz`), GitHub Actions CI *(old Epic 1, issues
  001–003, 005–009, 012–014 — unchanged)*
- **Supabase Auth** (replaces Clerk): asymmetric JWT verification via JWKS,
  `require_user` dependency, `GET /v1/me` with lazy landlord upsert
  *(issues 004, 010, 011, 015 — rewritten, same numbers)*
- Core tables only: `landlords`, `properties`, `tenants`, `conversations`,
  `messages` (append-only), `audit_log` (append-only), LangGraph checkpoint
  tables. **The audit log is a v1 feature, not hardening.**
- LangGraph graph: identify_property → load_context → classify_intent →
  classify_severity → draft_response → interrupt (approval) → send.
  Emergency branch: Twilio voice call to landlord + immediate safety SMS
  (no approval gate). *(old Epics 3 + 4)*
- Severity rubric v1, frozen prompts v1, LangSmith tracing from node one
- 10 eval scenarios (emergency / urgent / routine / refusal),
  `pytest -m eval` against the real API, run in CI on any change touching
  prompts or rubric
- Twilio: number provisioning, signed inbound webhook (persist before
  process), outbound send, **A2P 10DLC / CASL registration filed in week 1**
- Dashboard: approval queue wired to the real API (Brownstone design,
  `docs/mockups/04`) — approve with 5-second undo, edit, reasoning trace
- Cost metering: tokens + cost recorded per message from message one

### Out

- RLS, other landlords, billing, mobile, Inngest, MMS/photos, trust-ladder
  auto-send (trust *metrics* are recorded; auto-send stays off)

### Risks

- A2P/CASL registration lead time — file it first, it gates real SMS
- Severity rubric quality — false-negative emergencies are the catastrophic
  case; evals first, prompt cleverness second
- Scope creep from the old roadmap — anything not needed for the gate demo
  waits for M2

---

## Milestone 2 · Multi-landlord

**Absorbs:** old Epics 2 (schema + RLS), 5 (trust ladder), 6 (onboarding).
**Gate:** *A stranger signs up, onboards a property, forwards their tenant line, and runs the full loop with zero founder intervention — and the RLS isolation suite proves Landlord A cannot read Landlord B's data, for every multi-tenant table.*

### In

- Remaining tables (`trust_metrics`, `notifications`, `push_tokens`) +
  RLS on every multi-tenant table + cross-tenant isolation test suite
  *(old Epic 2 — unchanged in substance)*
- `auth.users` → `landlords` Postgres trigger (replaces Clerk webhooks #015)
- Trust ladder live: per-(property, severity) approval tracking unlocks
  routine auto-send; always revocable; emergency/urgent never auto-send in v1
- Self-serve onboarding: property + Twilio number provisioning, house rules,
  voice-profile capture (tone questionnaire + sample replies)
- Landing page live (Brownstone, `docs/mockups/05`) with the interactive
  triage demo
- 5–10 design-partner landlords recruited; every production
  misclassification becomes an eval case

### Out

- Payments, mobile app, trade scheduling (trust ladder LV3+)

---

## Milestone 3 · Money

**Absorbs:** old Epic 7 (billing) + the launch slice of 8–9.
**Gate:** *A landlord you've never met pays for a second door.*

### In

- Stripe: first door free, $19/door/mo after (pricing from the mockups —
  validate against metered LLM + Twilio cost per door before launch)
- Subscription state on `landlords`, dunning, cancel flow
- Unit-economics query: revenue per door vs. cost per door, from the
  metering that's existed since milestone 1
- Production hardening pass: backup/restore drill, incident runbook,
  rate limits on public endpoints

### Out

- Everything below

---

## Cut or deferred (and what brings each back)

| Item | Status | Comes back when |
|---|---|---|
| Clerk | **Cut** (ADR-1) | Org/team features or enterprise SSO required |
| Inngest | **Deferred** (ADR-2) | Webhook retries / background-task losses observed |
| Mobile app (old Epic 8) | Deferred | Paying landlords ask; dashboard is mobile-first meanwhile |
| MMS / photo handling | Deferred | Design partners hit it weekly (they will — pencil for M2.5) |
| Trade scheduling (trust LV3) | Deferred | Trust LV2 proven in production |
| SOC 2 | Deferred | First property-management company asks |
| Multi-region | Effectively never | Beyond-North-America expansion only |

## Scaling triggers

Lifted from [`architecture.md`](./architecture.md) §11 — scale work starts
when a number fires, not when it feels professional: durable queue on webhook
losses; indexes → compute → replica on p95 > 300 ms; second Fly machine at
~50 msg/min sustained; SOC 2 on enterprise ask.

---

## Issue bookkeeping

- Phase-1 issue specs in `phase-1/issues/` remain valid except **004, 010,
  011, 015 — rewritten for Supabase Auth** (same numbers, renamed files).
  The GitHub issues on `LaithAlz/stoop-backend` need matching edits.
- Old Epic 2–4 specs fold into Milestones 1–2 as mapped above; write new
  child issues per milestone as work starts, not all up front.
