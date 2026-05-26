# Stoop

AI-powered property management for independent landlords.

## Monorepo structure

```
apps/
  api/      FastAPI backend — auth, tenants, AI agent layer
  web/      TanStack Start web app — landlord dashboard (Cloudflare Workers)

packages/   Shared code (types, utils) — added as needed

docs/
  plans/    Phase-by-phase build plans and issue specs
```

## Apps

| App | Stack | Deploy |
|-----|-------|--------|
| `apps/api` | Python 3.12, FastAPI, async SQLAlchemy, Alembic | Fly.io (`yyz`) |
| `apps/web` | TypeScript, TanStack Start, shadcn/ui, Bun | Cloudflare Workers |

## Getting started

### API

```bash
cd apps/api
uv sync
cp .env.example .env   # fill in values
uv run uvicorn app.main:app --reload
```

### Web

```bash
cd apps/web
bun install
bun run dev
```

## Development workflow

Use `/ship <description>` in Claude Code to build any feature end-to-end:
branch → build → commit → PR → CI → review → merge.

## Phases

- **Phase 1** — Backend foundation (FastAPI + Supabase + Clerk + Fly.io) → [`docs/plans/phase-1`](docs/plans/phase-1/)
- Phase 2+ — Schema, RLS, agent layer, workers, billing, mobile

## Links

- Repo: [github.com/LaithAlz/stoop-backend](https://github.com/LaithAlz/stoop-backend)
- API (dev): `https://stoop-dev.fly.dev`
