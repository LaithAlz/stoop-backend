---
title: "[EPIC] Phase 1: Backend Foundation"
labels: ["epic", "phase-1"]
milestone: "Phase 1: Backend Foundation"
---

# Phase 1: Backend Foundation

A deployed, authenticated FastAPI app on Fly.io with Postgres, ready for the agent layer in Phase 2.

## Goal

By the end of Phase 1: `GET /v1/me` returns the authenticated landlord's profile, called from a real Clerk session, against a production Fly.io deployment, with CI passing on every PR.

## Why this phase exists separately

Foundation work is procedural and error-prone if rushed. Doing it as its own phase means multi-tenancy patterns, auth context, and deployment are correct before any business logic depends on them.

## Scope

**In scope:** Python 3.12 backend, FastAPI, async SQLAlchemy, Pydantic v2, Supabase Postgres, first migration (`landlords` table), Clerk JWT auth, first authenticated endpoint, Docker + Fly.io deploy, GitHub Actions CI, structured logging + Sentry.

**Out of scope:** Agent / LangGraph (Phase 2-3), other tables (Phase 2), RLS policies (Phase 2/5), Inngest workers (Phase 4), Twilio / Anthropic / Stripe (Phase 4-5), mobile or web frontend (Phase 7-8).

## Acceptance criteria

- [ ] `GET /v1/me` returns the landlord profile when called with a real Clerk JWT
- [ ] First call lazily creates a `landlords` row; second call returns the same id (no duplicates)
- [ ] Deployed to `stoop-dev.fly.dev` in the `yyz` region
- [ ] `/healthz` returns 200, `/readyz` returns 503 when DB is down
- [ ] CI runs on every PR (ruff, mypy strict, pytest) and blocks merge on failure
- [ ] Pre-commit hooks installed locally (ruff, gitleaks)
- [ ] Alembic up → down → up cycle works against staging DB
- [ ] Sentry receives a deliberate test error from production
- [ ] Structured JSON logs visible in Fly logs with `request_id` correlation
- [ ] `.env.example` documents every required env var
- [ ] No secrets in repo (gitleaks clean)

## Child issues

- [ ] #1 — Initialize Python backend with uv
- [ ] #2 — Configure ruff, mypy, pre-commit
- [ ] #3 — Create Supabase project
- [ ] #4 — Create Clerk application
- [ ] #5 — FastAPI app factory + health endpoints
- [ ] #6 — Settings module with pydantic-settings
- [ ] #7 — Structured logging + Sentry + request_id
- [ ] #8 — Alembic + landlords table migration
- [ ] #9 — Async SQLAlchemy session management
- [ ] #10 — Clerk JWT verification dependency
- [ ] #11 — GET /v1/me endpoint ← Phase 1 gate
- [ ] #12 — Dockerfile + docker-compose
- [ ] #13 — Fly.io deploy
- [ ] #14 — GitHub Actions CI
- [ ] #15 — Clerk webhook for user lifecycle (stretch)

(Update issue numbers after creation.)

## Risks

<details>
<summary>Known footguns to watch for</summary>

- **Clerk JWT verification.** Use JWKS public-key verification, not the secret key. The Python SDK has docs but the verification flow is easy to get wrong. Issue #10 details.
- **Supabase pooler vs direct connection.** Use the transaction-mode pooler on port 6543, not the direct connection on 5432. Direct connections exhaust the pool fast under any scaling.
- **Fly.io secrets vs env vars.** Secrets are encrypted and invisible after setting; env vars in `fly.toml` are committed. Anything sensitive → secrets.
- **Free-tier sleep.** Supabase pauses after 1 week of inactivity. Set a reminder or upgrade to Pro when you're using it daily.

</details>

## Definition of done

1. All non-stretch child issues (1-14) closed
2. All acceptance criteria checked
3. 5-minute screencast of `/v1/me` working from production (optional, recruiting artifact)

## After Phase 1

Phase 2 (Schema + RLS) starts with designing the full Postgres schema on paper before writing migrations.
