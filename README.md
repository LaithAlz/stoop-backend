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
apps/api    Python 3.12 · FastAPI · LangGraph agent · Fly.io   (being built — see issues)
apps/web    TanStack Start · shadcn/ui · Cloudflare Workers    (marketing site + dashboard)
docs/       The source of truth — numbered folders, see docs/README.md
CLAUDE.md   Rules and doc-map for AI coding sessions (apps/api/CLAUDE.md for backend)
```

## Quickstart

```bash
# web (marketing site + dashboard)
cd apps/web && bun install && bun run dev    # → localhost:8080

# api — being built issue-by-issue; see apps/api/CLAUDE.md for commands
```

## Building

Work is tracked as GitHub issues with acceptance criteria, organized into
release-train milestones (Train 1 → 3). The build order and feature map:
[`docs/04-roadmap/release-train.md`](docs/04-roadmap/release-train.md).

To implement with an AI session: *"Read CLAUDE.md, then implement issue #N."*

## Status

- Planning: complete (architecture, schema, contracts, rubric + evals, roadmap, pricing, GTM)
- Waitlist live at `/early-access` (pending domain + D1 setup — issue #114)
- Backend: Train 1 not started — first issue: [#1](https://github.com/LaithAlz/stoop-backend/issues/1)
