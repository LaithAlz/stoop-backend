---
title: "feat(backend): async SQLAlchemy session and real DB health check"
labels: ["phase-1", "type-implementation", "database", "size-s"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Set up an async SQLAlchemy engine, session factory, and FastAPI dependency. Wire `/readyz` to a real DB ping.

## Why this matters

Every authenticated endpoint needs a DB session. Doing this once, correctly, with proper async lifecycle prevents session leaks later.

## Acceptance criteria

- [ ] `backend/app/db/session.py` exposes a module-level async engine (one per process, not per-request)
- [ ] Engine uses asyncpg driver (URL transformed from `postgresql://` to `postgresql+asyncpg://`)
- [ ] Engine has reasonable pool sizing for Fly machines
- [ ] `get_session` async generator yields `AsyncSession`, commits on success, rolls back on exception
- [ ] `/readyz` performs a real DB ping (`SELECT 1`) — 200 if up, 503 with diagnostic info if down
- [ ] Test: with valid DB → `/readyz` returns 200
- [ ] Test: temporarily break the URL → `/readyz` returns 503

## Out of scope

- No SQLAlchemy ORM models yet — raw SQL via `text()` is fine for Phase 1
- No service-role connection yet — Phase 5
- No RLS session-variable setting yet — issue #10
- No connection retry logic — Phase 5+ if needed

## Effort & dependencies

- **Effort:** S (2-4 hours)
- **Blocks:** #10, #11
- **Blocked by:** #6, #8

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Session lifecycle.** When does a session start, commit, close? In FastAPI: per-request. Begin on request, commit if no exception, rollback on exception, close at end. Sketch the generator.

2. **Pool sizing.** Calculate: pool_size × N machines × N processes (web + worker) = total connections. Supabase free tier ~60. Don't oversubscribe.

3. **Where does the engine live?** Module-level singleton — one per process. Don't create engines per-request.

4. **What does `/readyz` check?** Just `SELECT 1`. Don't query a table — that couples readiness to schema state.

</details>

<details>
<summary><b>Hints</b></summary>

- `sqlalchemy.ext.asyncio` exports `create_async_engine`, `AsyncSession`, `async_sessionmaker`
- The session factory: `async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)`
- `expire_on_commit=False` is important — otherwise accessing attributes after commit triggers a re-fetch
- `pool_pre_ping=True` adds ~1ms per checkout but catches dead connections cleanly. Worth it for Supabase.
- `pool_recycle=300` recycles connections every 5 min — short enough to survive Supabase's idle timeout
- For the health check, separate `async with engine.connect() as conn` (raw connection, no session) — lighter weight than going through a session
- The FastAPI dependency pattern: an async generator function that yields the session inside a `try/except/finally`

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't use psycopg2 or psycopg3 sync drivers anywhere. Whole stack is async.
- Don't try to share an engine across processes (e.g., multi-worker uvicorn). asyncio engines are tied to the event loop.
- If you see "too many connections" errors, the pooler URL is probably wrong (port 5432 instead of 6543, or missing the pooler host)
- "this session is in 'inactive' state" errors usually mean a previous error wasn't rolled back. Make sure your dependency catches exceptions.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my async SQLAlchemy setup in `app/db/session.py`:
> 1. Is pool sizing reasonable for a Fly machine with 1 CPU / 1024MB?
> 2. Is `expire_on_commit=False` right? Will I have stale data issues?
> 3. Will Supabase's pooler (port 6543) play nice with this config?
> 4. Is the `get_session` dependency handling concurrent in-request usage correctly?

</details>
