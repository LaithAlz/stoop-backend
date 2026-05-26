---
title: "ci(repo): GitHub Actions workflow for lint, type, test on every PR"
labels: ["phase-1", "type-deployment", "ci", "size-s"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

GitHub Actions workflow that runs ruff, mypy strict, and pytest on every PR. Blocks merge if any fail.

## Why this matters

Manual discipline isn't enough at solo scale. CI is the safety net. Branch protection means even if you forget to run checks locally, they run before merge.

## Acceptance criteria

- [ ] `.github/workflows/ci.yml` exists
- [ ] Triggers on `pull_request` and `push` to `main`
- [ ] Spins up a Postgres service for tests that need a DB
- [ ] Runs against the backend folder: `ruff check`, `ruff format --check`, `mypy app`, `alembic upgrade head` (against the test Postgres), `pytest -m "not eval"`
- [ ] Caches uv's package cache across runs (faster CI)
- [ ] CI completes in under 4 minutes
- [ ] Branch protection on `main` configured to require this workflow to pass before merge
- [ ] Branch protection requires linear history (squash merge only)
- [ ] Branch protection blocks force pushes to `main`

## Out of scope

- No eval suite runs yet — Phase 3
- No deploy automation — manual `fly deploy` is fine until Phase 9
- No security scanning (Snyk, Dependabot) — could add later
- No coverage gates — no code to cover meaningfully

## Effort & dependencies

- **Effort:** S (2-3 hours)
- **Blocks:** None (Phase 1 can technically gate without CI, but really don't)
- **Blocked by:** #2

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **One workflow file or multiple?** Phase 1: one file with one job covers everything. As you add more (frontend in Phase 7, evals in Phase 3), split into multiple jobs in the same workflow or separate workflow files.

2. **Service container for Postgres or skip DB tests in CI?** Run Postgres as a GitHub Actions service container. Real Postgres in CI catches things SQLite mocks miss.

3. **What's in scope to run in CI?** Lint, type, test. Don't include slow things (eval suite, integration tests against real Anthropic/Twilio).

</details>

<details>
<summary><b>Hints</b></summary>

- Use `astral-sh/setup-uv@v3` action — handles uv install + cache
- For Postgres: GitHub Actions `services:` block with `image: postgres:15` and health check
- Set `DATABASE_URL` env for the workflow to point at the service container
- `working-directory: backend` on each step is cleaner than `cd backend && ...`
- For pre-commit hooks running in CI, the simplest path is calling the same commands directly (ruff, mypy) — no need to install pre-commit itself in CI
- `actions/checkout@v4` with default options is fine for Phase 1 — no submodules, no LFS
- For branch protection, set it up via GitHub UI: Settings → Branches → Add rule for `main`. Require status checks: select the CI workflow.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- The Postgres service container takes ~5s to be ready. Use a health check or sleep, or it'll fail intermittently.
- `services.postgres.ports` needs `5432:5432` mapping to be reachable
- `gen_random_uuid()` requires Postgres 13+. Use `postgres:15` image.
- Don't try to use the actual Supabase dev DB from CI — slow, eats your free tier, and isolation issues if multiple PRs run concurrently
- `mypy` in CI may hit cache issues — set `MYPY_CACHE_DIR: .mypy_cache` and cache that directory
- `--frozen` on `uv sync` in CI is important — without it CI may resolve to different deps than your lockfile

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my `.github/workflows/ci.yml`:
> 1. Is the Postgres service container set up correctly?
> 2. Am I caching the right things to speed up CI?
> 3. Are the steps ordered to fail fast on cheap checks first?
> 4. Anything missing that I'll want before Phase 2 (e.g., separate jobs for different packages)?

</details>
