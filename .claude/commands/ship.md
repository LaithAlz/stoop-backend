Ship a feature end-to-end: branch → build → commit → PR → CI → review → merge.

## What to build

$ARGUMENTS

## Workflow

Follow every step in order. Do not skip or reorder.

### 1. Branch

Create a branch off `main` using the pattern `<type>/<short-description>`:
- `feat/` — new functionality
- `fix/` — bug fixes
- `chore/` — setup, config, tooling
- `ci/` — workflow changes
- `docs/` — documentation only

Branch name: lowercase, hyphenated, ≤40 chars. Example: `feat/clerk-jwt-verification`.

```
git checkout main && git pull origin main
git checkout -b <branch-name>
```

### 2. Orient

Before writing a line of code:
- Identify which app the change lives in (`apps/api` or `apps/web`)
- Read relevant existing files — understand the patterns before adding to them
- Scope your change to exactly what was asked. No bonus refactors.

### 3. Build

Implement the feature. Keep changes contained to their app. Do not reach across app boundaries unless the task explicitly requires it.

Repo layout:
```
apps/api/     FastAPI backend (Python 3.12, uv, async SQLAlchemy, Alembic, Clerk)
apps/web/     TanStack Start frontend (TypeScript, Bun, shadcn/ui, Cloudflare Workers)
packages/     Shared code — only add here if genuinely reused across apps
docs/   Phase planning docs — read-only reference
```

### 4. Commit

Short, imperative, direct. Subject line ≤72 chars. No body unless the why is genuinely non-obvious.

**Good:**
- `add Clerk JWT verification dependency`
- `fix session rollback on integrity error`
- `configure ruff and mypy strict mode`

**Never:**
- No "Claude", "AI", "Co-authored", "as requested", "Update code to…"
- No trailing period
- No filler

Stage specific files — never `git add .` blindly.

### 5. Push + PR

```bash
git push -u origin <branch-name>
gh pr create --repo LaithAlz/stoop-backend \
  --title "<imperative title>" \
  --body "<body>"
```

PR body must follow the template (`.github/PULL_REQUEST_TEMPLATE.md`):
- **What** — one sentence on what changed
- **Why** — one sentence on why, link the issue (`Closes #N`)
- **Test plan** — checklist of how to verify

### 6. CI

Check if workflows exist:

```bash
gh workflow list --repo LaithAlz/stoop-backend
```

If any exist, wait for all checks to pass:

```bash
gh pr checks <PR-number> --repo LaithAlz/stoop-backend --watch
```

If a check fails: read the output, fix the code, push a new commit, wait again. Do not proceed until CI is green.

### 7. Review

Spawn a review agent with this prompt:

> You are a senior engineer reviewing a PR for the Stoop monorepo (FastAPI API + TanStack web app).
>
> Run these to get context:
> ```
> gh pr view <PR-number> --repo LaithAlz/stoop-backend
> gh pr diff <PR-number> --repo LaithAlz/stoop-backend
> ```
>
> Review across five dimensions:
> 1. **Correctness** — does it do what the PR says? Any logic bugs or missed edge cases?
> 2. **Security** — auth bypasses, injection risks, secrets exposure, PII leaks?
> 3. **Async correctness** (API) — session lifecycle, no sync calls in async context, no shared mutable state?
> 4. **Type safety** — mypy strict (API) / TypeScript strict (web), no missing annotations?
> 5. **Scope** — does it do more than asked? Flag but don't block for cosmetic issues.
>
> Output a structured report:
> - **BLOCKING** — must fix before merge
> - **ADVISORY** — worth fixing, won't block
> - If no blocking issues: "LGTM — ready to merge"

If BLOCKING issues exist: fix them, push new commits, re-run CI, re-run the review. Repeat until LGTM.

### 8. Merge

```bash
gh pr merge <PR-number> --repo LaithAlz/stoop-backend --squash --delete-branch
```

Confirm the merge succeeded and the branch is gone.

---

## Hard rules

- Never commit directly to `main`
- Never merge with a failing CI check
- Never merge without review agent LGTM
- Never `git add .` — stage files explicitly
- Never skip steps
