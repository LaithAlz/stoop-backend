# Stoop — monorepo guide

AI-powered tenant-maintenance handling for landlords. Tenants text one
number; Stoop sorts every message, drafts replies in the landlord's voice,
and only rings the landlord's phone for a true emergency.

## Layout

- `apps/api` — Python 3.12 / FastAPI / LangGraph backend (Fly.io). See `apps/api/CLAUDE.md`.
- `apps/web` — TanStack Start + shadcn/ui dashboard & marketing site (Cloudflare Workers, Bun).
- `docs/` — **the source of truth.** Code follows these docs, not vice versa.
- `docs/mockups/` — HTML design mockups (Brownstone = dashboard direction; the live site uses Heritage).

## Doc map — read before touching the related area

| Doc | Owns |
|---|---|
| `docs/03-engineering/architecture.md` | stack, system diagram, ADRs, scaling triggers |
| `docs/03-engineering/schema-v1.md` | **canonical table/column names — never invent names** |
| `docs/03-engineering/api-contracts.md` | endpoint shapes, error envelope, pagination |
| `docs/02-product/severity-rubric-v1.md` | classification rubric **v1.0, frozen** |
| `docs/02-product/eval-scenarios-v1.md` | the 10 eval cases + scoring rules |
| `docs/02-product/conversation-model.md` | channel vs case, lifecycle, stale-draft rule |
| `docs/02-product/emergency-prefilter.md` | Tier-0 filter, degraded mode, escalation chain |
| `docs/04-roadmap/release-train.md` | release-train roadmap (Trains 1–3) |
| `docs/01-strategy/business-model.md` / `three-year-plan.md` | pricing, segments, horizons |

GitHub issues on `LaithAlz/stoop-backend` carry per-task acceptance
criteria; `docs/03-engineering/issue-specs/` has the detailed specs for #1–#15.

## Commands

- web: `cd apps/web && bun install && bun run dev` · build `bun run build` · lint `bun run lint`
- api: see `apps/api/CLAUDE.md` (uv-based)

## Rules that never bend (project-wide)

1. **The emergency line is never paywalled, throttled, or gated.**
2. **`messages` and `audit_log` are append-only.** No UPDATE/DELETE, ever,
   anywhere — the migrations revoke the grants; code must not fight that.
3. **Nothing sends to a tenant or vendor without landlord approval**, except
   emergency safety instructions. Auto-send exists only via the trust
   ladder, only for `routine`, per `(property, severity)`.
4. **The rubric is embedded verbatim** (`severity-rubric-v1.md` → checksum
   test). A prompt or rubric change = new version file + full eval run.
5. **Never log JWTs, tenant phone numbers, or message bodies** in app logs,
   Sentry, or error messages. The `auth_user_id` / row uuids are enough.
6. **Schema names come from `schema-v1.md`.** A new column means editing
   that doc first, then the migration.
7. **Analytics discipline (ADR-5):** PostHog identifies by landlord uuid
   only — no emails/names/phones/message bodies in event properties;
   session replay stays off. **Feature flags never gate safety behavior**
   (emergency path, rubric, approval requirements) — flags are for
   rollouts, pricing cohorts, and experiments only.
8. Customer-facing copy: plain English (never "triage"), no legal/LTB
   mentions on marketing pages, never "founding/cohort/spot counts" — say
   "early access". Prices: free Emergency Line / $10 Full Plan /
   $5 early-access (grandfathered) / PMs $1.50/door.

## Git

- Conventional-ish commits (`feat(web): …`, `docs: …`). Push to `main` is
  normal for docs; app code goes through the `/ship` flow (branch → PR →
  CI) once CI exists (#14).
