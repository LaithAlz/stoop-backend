# Stoop · Phase 1 Issues

15 GitHub issues for Phase 1 (Backend Foundation, ~2 weeks). One issue = one PR.

Each issue contains **what** to build and **why** — not how. Code-level details, gotchas, and starter patterns are tucked inside collapsible `<details>` blocks so you can ignore them by default and only pop them open when stuck.

## The format

Every issue has:

- **Goal** — one sentence
- **Why this matters** — what it unblocks
- **Acceptance criteria** — checklist a reviewer would verify
- **Out of scope** — what NOT to do in this ticket
- **Effort + dependencies** — sizing and what blocks it
- `<details>` **Hints** — collapsed by default. Open if you want a nudge.
- `<details>` **Common gotchas** — collapsed. Things people get wrong.
- `<details>` **Review prompts for Claude Code** — what to ask once your PR is up

Open the `<details>` blocks only when you want to. The goal: design and implement on your own, get help when you're stuck, ask Claude Code for review.

## How to use with Claude Code

### Option 1 — Bulk create (recommended)

In your repo, run this prompt against Claude Code:

> Read every `.md` file in `docs/plans/phase-1/issues/`. For each one:
> 1. Parse the YAML frontmatter (title, labels, milestone).
> 2. Create a GitHub issue using `gh issue create` with the file's full markdown body (excluding the frontmatter) as the body.
> 3. Apply the labels and milestone from the frontmatter.
> 4. Wait between issues to avoid rate limiting.
>
> Before starting, run `gh label list` to confirm the labels exist. If any labels listed in the frontmatter are missing, create them first using sensible colors (phase = blue, type-* = green, size = purple, gate = red, stretch = gray).
>
> Also create the milestone "Phase 1: Backend Foundation" if it doesn't exist.
>
> After creating all issues, also create one parent issue from `docs/plans/phase-1/EPIC.md` and link the 15 children to it via task list checkboxes.

Claude Code handles label creation, milestone, issue body, and EPIC linking in one go.

### Option 2 — Manual

Read an issue, click "New Issue" in GitHub, paste the body, apply labels manually. Slower but no setup.

### Option 3 — Script

If you want to run it yourself, use `scripts/create-issues.sh` (included). Requires `gh` CLI and `yq` for YAML parsing.

## The issues

1. [001](./issues/001-init-python-project.md) — Initialize Python backend with uv
2. [002](./issues/002-dev-tooling.md) — Configure ruff, mypy, pre-commit
3. [003](./issues/003-supabase-setup.md) — Create Supabase project
4. [004](./issues/004-supabase-auth-setup.md) — Configure Supabase Auth
5. [005](./issues/005-fastapi-app-factory.md) — FastAPI app factory + health endpoints
6. [006](./issues/006-settings-module.md) — Settings module with pydantic-settings
7. [007](./issues/007-logging-and-sentry.md) — Structured logging + Sentry + request_id
8. [008](./issues/008-alembic-and-landlords.md) — Alembic + landlords table migration
9. [009](./issues/009-async-sqlalchemy-session.md) — Async SQLAlchemy session management
10. [010](./issues/010-supabase-jwt-dependency.md) — Supabase JWT verification dependency
11. [011](./issues/011-me-endpoint.md) — GET /v1/me endpoint
12. [012](./issues/012-dockerfile-and-compose.md) — Dockerfile + docker-compose
13. [013](./issues/013-fly-deploy.md) — Fly.io deploy
14. [014](./issues/014-github-actions-ci.md) — GitHub Actions CI
15. [015](./issues/015-auth-user-lifecycle.md) — auth.users → landlords trigger sync (stretch)

## Sequencing

```
001 → 002 → 005 → 006 → 007
       │
       ├──► 008 → 009 → 010 → 011 (gate)
       │                       │
       │                       └─► 015 (optional)
       │
       └──► 014

005 → 012 → 013 (gate)
```

Run order suggestion:

| Days | Issues | Notes |
|---|---|---|
| 1 | 001, 002, 003, 004 | All setup, get account access |
| 2 | 005, 006 | App skeleton |
| 3 | 007, 008 | Logging + first migration |
| 4 | 009, 010 | DB session + auth (the meaty ones) |
| 5 | 011 | `/v1/me` — the gate |
| 6 | 012, 013 | Docker + deploy to Fly |
| 7 | 014 | CI pipeline |
| 8-10 | 015 (stretch) + buffer | Cleanup, screencast, prep Phase 2 |

## When you finish

Phase 1 is done when the EPIC's acceptance criteria are checked. Mark the milestone as closed. Take a screenshot of `curl https://stoop-dev.fly.dev/v1/me` returning your profile — recruiting artifact.

Then come back for the Phase 2 plan.
