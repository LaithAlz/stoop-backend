---
title: "chore(backend): initialize Python project with uv"
labels: ["phase-1", "type-setup", "size-xs"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Initialize the `backend/` Python project with `uv` and the core production + dev dependencies.

## Why this matters

Every other Phase 1 issue depends on a working Python project skeleton. Locked deps from day one prevents version drift later.

## Acceptance criteria

- [ ] `backend/pyproject.toml` exists with project metadata
- [ ] Python 3.12 pinned (`.python-version` file)
- [ ] Production deps installed: FastAPI, Uvicorn, Pydantic v2, pydantic-settings, SQLAlchemy 2.0, asyncpg, Alembic, httpx, structlog, Clerk SDK, Sentry SDK
- [ ] Dev deps installed in their own group: pytest, pytest-asyncio, respx, ruff, mypy
- [ ] `uv sync` runs clean
- [ ] `uv.lock` committed
- [ ] `.gitignore` covers `.venv/`, `__pycache__/`, `.env`, `*.pyc`
- [ ] `backend/README.md` exists with a short "what is this / how to run dev" blurb (5 lines max)

## Out of scope

- Don't add LangGraph or Anthropic SDK — Phase 3
- Don't add Inngest, Twilio, Stripe SDKs — Phase 4-5
- Don't write any application code — that's issue #5

## Effort & dependencies

- **Effort:** XS (30-60 min)
- **Blocks:** All other Phase 1 issues
- **Blocked by:** None

---

<details>
<summary><b>Hints (open if stuck)</b></summary>

- `uv init --python 3.12` scaffolds `pyproject.toml`
- `uv add <pkg1> <pkg2>` adds to main deps; `uv add --dev <pkg>` adds to dev group
- For extras, the syntax is `uv add 'sentry-sdk[fastapi]'` with quotes
- Verify imports work with a one-liner before closing the issue

</details>

<details>
<summary><b>Common gotchas</b></summary>

- If `uv` can't find Python 3.12, install it first: `mise install python@3.12` or `pyenv install 3.12`
- Don't use `pip install` after `uv` is set up — they fight over state
- Don't commit `.venv/` — `.gitignore` it
- `uv.lock` IS committed — it's how reproducibility works

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

When your PR is up, ask Claude Code:

> Review my `backend/pyproject.toml` and `uv.lock`:
> 1. Are any deps pinned to wrong versions for Phase 1?
> 2. Are deps split correctly between main and dev groups?
> 3. Is anything missing that I'll regret not having from day one?

</details>
