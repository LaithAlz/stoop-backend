---
title: "feat(backend): FastAPI app factory with healthz and readyz endpoints"
labels: ["phase-1", "type-implementation", "size-s"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Build a minimal FastAPI app with `/healthz` (liveness) and `/readyz` (readiness) endpoints that runs locally and is ready to containerize.

## Why this matters

Every other issue depends on a working FastAPI skeleton. Health checks are needed for Fly.io to know when machines are ready to receive traffic.

## Acceptance criteria

- [ ] `backend/app/main.py` exports `app` from an app factory function
- [ ] `GET /healthz` returns 200 with `{"status": "ok"}` immediately (no dependencies)
- [ ] `GET /readyz` returns 200 when the app can serve traffic, 503 otherwise (for now: always 200, real DB check comes in #9)
- [ ] Both endpoints are public (no auth)
- [ ] Both endpoints are excluded from CORS preflight requirements
- [ ] `uv run uvicorn app.main:app --reload` starts the server on port 8000
- [ ] Folder structure matches `backend/AGENTS.md` (`app/routers/`, `app/db/`, etc — even if some folders are empty for now)
- [ ] At least one smoke test in `tests/` validates `/healthz` returns 200

## Out of scope

- No CORS configuration yet — Phase 7 when mobile connects
- No request_id middleware yet — issue #7
- No real DB health check yet — issue #9 wires it
- No OpenAPI customization

## Effort & dependencies

- **Effort:** S (2-3 hours)
- **Blocks:** All downstream implementation issues
- **Blocked by:** #1, #2

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Why two endpoints?** `/healthz` is liveness ("is the process alive — restart if not"). `/readyz` is readiness ("should I receive traffic"). Liveness shouldn't fail just because the DB is slow; readiness should.

2. **App factory vs module-level app.** Using `create_app()` lets you spin up multiple instances with different configs for testing. Cheap to set up now; painful to refactor later.

3. **What goes in `/readyz`?** Phase 1 answer: just a DB ping (in #9). Later phases: also check that critical deps respond. Rule: only check things whose failure means "don't route me traffic."

</details>

<details>
<summary><b>Hints</b></summary>

- The factory pattern: a function that returns a configured `FastAPI` instance, called once at module level (`app = create_app()`)
- For testing FastAPI without running uvicorn, use `httpx.AsyncClient` with `ASGITransport(app=app)`
- Use `tags=[...]` on routers to group endpoints in the auto-generated `/docs` page
- Return a `JSONResponse` directly from `/readyz` so you can return 503 with a structured body later without refactoring

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't put DB queries in `/healthz` — that endpoint should be cheap. Fly hits it every 15 seconds.
- Don't add auth to health endpoints. Fly's health checker doesn't authenticate.
- Don't accidentally make `app` lazy/None at import time. `app.main:app` is what uvicorn imports; it has to exist at module load.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my FastAPI app factory in `app/main.py` and router in `app/routers/health.py`:
> 1. Is the factory pattern correct for testability?
> 2. Should `/readyz` use response_model schemas instead of raw dicts?
> 3. Am I missing middleware I'll need later (CORS, request_id, exception handlers)?
> 4. Are my tests using the right async client pattern for FastAPI?

</details>
