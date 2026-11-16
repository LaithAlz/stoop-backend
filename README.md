# Stoop

Your tenants text. You sleep.

Stoop receives tenant maintenance texts, sorts them by urgency, drafts
replies in the landlord's voice for one-tap approval, lines up the
landlord's own tradespeople, and rings the landlord's phone — with an
escalation chain — when something genuinely can't wait. Everything is
logged to a tamper-evident record. The emergency line is free, forever.

**📚 All documentation: [`docs/README.md`](docs/README.md)** — strategy,
product spec, engineering, roadmap, go-to-market, legal. Start there.

## Repo layout

```
apps/api    Python 3.12 · FastAPI · LangGraph agent · Fly.io   (backend, tests, and evals)
apps/web    TanStack Start · shadcn/ui · Cloudflare Workers    (marketing site + dashboard)
apps/mobile Expo · React Native                                 (landlord mobile app)
docs/       The source of truth — numbered folders, see docs/README.md
CLAUDE.md   Rules and doc-map for AI coding sessions (apps/api/CLAUDE.md for backend)
```

## Quickstart

```bash
# web (marketing site + dashboard)
cd apps/web && bun install && bun run dev    # → localhost:8080

# api
cd apps/api && uv sync && uv run pytest       # see apps/api/CLAUDE.md for the full command list
```

## Building

Work is tracked as GitHub issues with acceptance criteria, organized into
release-train milestones (Train 1 → 3). The build order and feature map:
[`docs/04-roadmap/release-train.md`](docs/04-roadmap/release-train.md).

To implement with an AI session: *"Read CLAUDE.md, then implement issue #N."*

## Status

- **Backend (`apps/api`):** built and under test. FastAPI and a LangGraph agent, with a 20-scenario eval suite in `apps/api/evals` and a row-level-security isolation test matrix.
- **Web and mobile:** landlord dashboard in `apps/web`, mobile app in `apps/mobile`.
- **Roadmap:** Train 1 (core loop) is mostly done, Train 2 is in progress, Train 3 has not started. See the milestones.
- **Not yet deployed with real tenants.** The early-access waitlist page is tracked in issue #114.
