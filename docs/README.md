# Stoop — Documentation Index

Everything about this business and product, organized so you can find any
answer without asking anyone. Folders are numbered in reading order:
strategy → product → engineering → roadmap → go-to-market → legal.

## Start here (15 minutes)

1. [`01-strategy/three-year-plan.md`](01-strategy/three-year-plan.md) — where this is going and why
2. [`04-roadmap/release-train.md`](04-roadmap/release-train.md) — what's being built, in what order
3. [`03-engineering/architecture.md`](03-engineering/architecture.md) — how it works

---

## 01-strategy/ — why this business

| File | One line |
|---|---|
| [`three-year-plan.md`](01-strategy/three-year-plan.md) | The 3-act arc (Inbox → Desk → Network), horizons with gates not dates, hire/fundraise/kill triggers, standing metrics & risks |
| [`business-model.md`](01-strategy/business-model.md) | Competitive research (TurboTenant, Latchel, Mezo, EliseAI…), two segments, pricing (free Emergency Line / $10 Full / $5 early-access grandfathered / PM $1.50/door), traction playbook |

## 02-product/ — what the product does (precisely)

| File | One line |
|---|---|
| [`severity-rubric-v1.md`](02-product/severity-rubric-v1.md) | **Frozen v1.0.** The exact text the AI uses to classify Emergency/Urgent/Routine; the six signed-off judgment calls; changes require a new version + full eval run |
| [`eval-scenarios-v1.md`](02-product/eval-scenarios-v1.md) | The 10 test cases the AI must pass (3 emergency / 3 urgent / 2 routine / 2 refusal) + scoring rules; E/F failures block releases |
| [`conversation-model.md`](02-product/conversation-model.md) | Channel vs case, case lifecycle (open→resolved→reopen), the one-pending-draft rule, message routing |
| [`emergency-prefilter.md`](02-product/emergency-prefilter.md) | The non-AI safety net: keyword Tier-0, degraded mode when the AI is down, the call-escalation chain when you don't answer |

## 03-engineering/ — how it's built

| File | One line |
|---|---|
| [`architecture.md`](03-engineering/architecture.md) | Stack + rationale, system diagram, agent graph, scaling triggers, ADR-1…5 (every big decision and why) |
| [`schema-v1.md`](03-engineering/schema-v1.md) | **Canonical DDL** — every table and column; nothing in code may invent a name |
| [`api-contracts.md`](03-engineering/api-contracts.md) | Every endpoint's request/response shape, error envelope, the approve/undo semantics |
| [`issue-specs/`](03-engineering/issue-specs/) | Detailed build specs for GitHub issues #1–#15 (+ EPIC) with hints and gotchas |

Also: `/CLAUDE.md` (repo rules for AI coding sessions) and
`/apps/api/CLAUDE.md` (backend conventions + the never-break agent rules).

## 04-roadmap/ — what ships when

| File | One line |
|---|---|
| [`release-train.md`](04-roadmap/release-train.md) | Trains 1–3 (v1.0→v3.0): every feature committed and ordered; what physics gates regardless of speed; GitHub milestones mirror this |

GitHub issues: https://github.com/LaithAlz/stoop-backend/issues (~82 open
across Train 1/2/3 milestones, each with acceptance criteria).

## 05-gtm/ — getting customers

| File | One line |
|---|---|
| [`design-partners.md`](05-gtm/design-partners.md) | The 10-pilot plan: who qualifies, where they are, the pitch, the offer, funnel math, pre-mortems |
| [`outreach-scripts.md`](05-gtm/outreach-scripts.md) | Word-for-word: warm intro, Kijiji DM, Reddit post, REI 30-second pitch, follow-up cadence |
| [`seo-content.md`](05-gtm/seo-content.md) | First two post outlines + the "Is it an emergency?" quiz spec (the rubric as a lead magnet) |
| [`video-plan.md`](05-gtm/video-plan.md) | 10-video library with full Higgsfield prompts per shot, guardrails, 3-session production order |

## 06-legal/ — the paperwork

| File | One line |
|---|---|
| [`terms-of-service.md`](06-legal/terms-of-service.md) | DRAFT (lawyer review flagged) — incl. not-an-emergency-service clause, price-lock, liability cap |
| [`privacy-policy.md`](06-legal/privacy-policy.md) | DRAFT (PIPEDA-oriented) — tenant data on landlord's behalf, no AI training, Canadian residency, subprocessors |
| [`pilot-kit.md`](06-legal/pilot-kit.md) | Tenant disclosure SMS/letter, one-page pilot agreement, the 30-min onboarding script |

## mockups/ — design references

Open any file in a browser. `index.html` is the gallery.
01–03 = three explored directions · **04 = Brownstone dashboard (the
chosen app design)** · 05 = Brownstone landing (superseded for marketing —
the live site uses the Heritage light design, see `apps/web`).

---

## "I want to…" quick paths

- **…understand the pricing** → `01-strategy/business-model.md` §2
- **…know why we chose X technology** → `03-engineering/architecture.md` Decision log (ADRs)
- **…see what the AI does with a message** → `02-product/severity-rubric-v1.md`, then `emergency-prefilter.md`
- **…start building** → `/CLAUDE.md`, then GitHub issue #1
- **…recruit a pilot landlord** → `05-gtm/outreach-scripts.md` + `06-legal/pilot-kit.md`
- **…know what's left besides code** → incorporation, domain, account sweep, A2P filing, lawyer pass, 5 validation conversations (see `05-gtm/design-partners.md` + the humans-only list in `/apps/api/CLAUDE.md`)
- **…make the marketing videos** → `05-gtm/video-plan.md` (execution-ready prompts)
