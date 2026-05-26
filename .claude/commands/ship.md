Ship a feature end-to-end: branch → build → commit → PR → CI → review → merge.

## What to build

$ARGUMENTS

## Workflow

Follow every step in order. Do not skip or reorder.

### 1. Branch

Create a branch off `main` using the pattern `<type>/<short-description>`:
- `feat/` for new functionality
- `fix/` for bug fixes
- `chore/` for setup, config, tooling
- `ci/` for workflow changes

Branch name must be lowercase, hyphenated, ≤40 chars. Example: `feat/clerk-jwt-verification`.

```
git checkout main && git pull origin main
git checkout -b <branch-name>
```

### 2. Build

Implement what was asked. Read relevant existing code first — understand the patterns before adding to them. Keep changes scoped to the task. Do not refactor unrelated code.

### 3. Commit

Commit messages are short, imperative, and direct. Subject line ≤72 chars.

**Good:**
- `add Clerk JWT verification dependency`
- `fix session rollback on integrity error`
- `configure ruff and mypy strict mode`

**Never:**
- No "Claude", "AI", "Co-authored", or "as requested"
- No trailing period
- No filler ("Update code to...", "Made changes to...")

Stage specific files — never `git add .` blindly.

### 4. Push + PR

Push the branch and open a PR:

```
git push -u origin <branch-name>
gh pr create --repo LaithAlz/stoop-backend \
  --title "<commit-subject-style title>" \
  --body "<body>"
```

PR body must include:
- **What** — one sentence on what changed
- **Why** — one sentence on why it was needed
- **Test plan** — checklist of how to verify it works

Keep it tight. No fluff.

### 5. CI

Check if any GitHub Actions workflows exist:

```
gh workflow list --repo LaithAlz/stoop-backend
```

If workflows exist, poll until ALL required checks pass:

```
gh pr checks <PR-number> --repo LaithAlz/stoop-backend --watch
```

If any check fails: stop, read the failure output, fix the code, push a new commit, and wait again. Do not proceed to review until CI is green.

If no workflows exist yet, skip this step and note it in the review.

### 6. Review

Spawn a review agent with this exact prompt:

> You are a senior engineer reviewing a pull request for the Stoop backend (FastAPI + async SQLAlchemy + Clerk + Fly.io). 
>
> PR: `gh pr view <PR-number> --repo LaithAlz/stoop-backend`
> Diff: `gh pr diff <PR-number> --repo LaithAlz/stoop-backend`
>
> Review for:
> 1. Correctness — does it do what the PR says? Any logic bugs?
> 2. Security — any auth bypasses, injection risks, secrets exposure, or PII leaks?
> 3. Async correctness — proper session lifecycle, no sync calls in async context, no shared mutable state?
> 4. Type safety — mypy strict compliance, no missing annotations?
> 5. Scope creep — does it do more than asked? Flag but don't block for cosmetic issues.
>
> Output: a structured report with BLOCKING issues (must fix before merge) and ADVISORY issues (nice to fix, won't block). If no blocking issues, say "LGTM — ready to merge."

If the review agent raises BLOCKING issues: fix them, push new commits, re-run CI, re-run the review agent. Repeat until LGTM.

### 7. Merge

Once CI is green and review says LGTM:

```
gh pr merge <PR-number> --repo LaithAlz/stoop-backend --squash --delete-branch
```

Confirm the merge succeeded and the branch is deleted.

---

## Hard rules

- Never commit directly to `main`
- Never merge with a failing CI check
- Never merge without review agent sign-off
- Never use `git add .` — stage files explicitly
- Never skip steps to go faster
