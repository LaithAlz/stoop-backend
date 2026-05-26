---
title: "feat(backend): GET /v1/me endpoint with get-or-create landlord"
labels: ["phase-1", "type-implementation", "auth", "size-s", "gate"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

`GET /v1/me` returns the authenticated landlord's profile. First call lazily creates the landlord record from Clerk claims; subsequent calls return the same record.

**This is the Phase 1 gate.** When this works in production (after #13), Phase 1 is done.

## Why this matters

The seam between Clerk (identity) and our database (application state). Every future authenticated endpoint will follow this pattern: verify JWT → look up local user → do business logic.

## Acceptance criteria

- [ ] `GET /v1/me` returns the landlord profile when called with a valid Clerk JWT
- [ ] First call for a new Clerk user → creates a `landlords` row → returns profile
- [ ] Subsequent calls → return the same `landlords.id` (no duplicates created)
- [ ] If the user's email in Clerk changes, the local record is refreshed on next read (lazy sync)
- [ ] Response is typed via Pydantic — fields include `id`, `email`, `full_name`, `timezone`, `subscription_tier`, `subscription_status`, `trial_ends_at`, `created_at`
- [ ] No `Authorization` header → 401 (handled by the dep from #10)
- [ ] Race condition handled: concurrent first-time requests don't both insert (no duplicate-key error to the user)
- [ ] Test: with a real Clerk JWT, calling `/v1/me` twice returns the same id
- [ ] Test: without auth, returns 401

## Out of scope

- No `PATCH /v1/me` for profile editing — Phase 5
- No subscription management — Phase 5
- No avatar upload — Phase 7+
- No notification preferences update — Phase 5

## Effort & dependencies

- **Effort:** S (3-4 hours)
- **Blocks:** Closes Phase 1 EPIC when deployed (after #13)
- **Blocked by:** #8, #9, #10

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Get-or-create pattern.** First call lazily creates the row. No separate "sign up" endpoint — sign-up is implicit (Clerk handles auth; we just need a row).

2. **Race condition.** Two concurrent first-time requests both check "exists?" both see no, both INSERT, second one fails on UNIQUE. Two ways to handle:
   - Catch IntegrityError → re-select
   - Use Postgres upsert: `INSERT ... ON CONFLICT (clerk_user_id) DO UPDATE SET ... RETURNING *`

   Upsert is cleaner — one query, atomic. Sketch it.

3. **What to return.** What does the mobile app need? Don't dump everything. Design the response schema.

4. **Email sync.** If Clerk's email differs from local, update local on read. Why? User changes their email in Clerk → app reflects without webhook. Simple lazy sync. Webhook-based sync comes in #15 (stretch).

</details>

<details>
<summary><b>Hints</b></summary>

- The upsert pattern: `INSERT INTO landlords (...) VALUES (...) ON CONFLICT (clerk_user_id) DO UPDATE SET email = EXCLUDED.email, full_name = COALESCE(EXCLUDED.full_name, landlords.full_name), updated_at = now() RETURNING *`
- Use `session.execute(text(...))` with `result.mappings().one()` to get a dict-style row when you don't have ORM models yet
- `response_model=MeResponse` on the route decorator gives you automatic Pydantic validation of the response
- Use `Depends(require_clerk_user)` and `Depends(get_session)` together — FastAPI handles the order
- structlog: `bind_contextvars(clerk_user_id=user.user_id)` at the top of the handler so all logs in this request get tagged

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't try to do the get-or-create in two queries (`SELECT` then `INSERT`). Race condition. Use upsert.
- `COALESCE(EXCLUDED.full_name, landlords.full_name)` preserves an existing full_name if Clerk sends a null. The other direction (Clerk sends a name, we want to update) is `EXCLUDED.full_name`.
- A GET with side effects (lazy create) is debatably REST-correct, but it's the cleanest UX. Document it. Don't overthink — Stripe, GitHub, and most modern APIs do this for "current user" endpoints.
- Don't return Clerk-specific fields (clerk_user_id) — keep that an internal detail.
- The audit log doesn't exist yet (Phase 5 adds it). Don't try to write to it.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my `/v1/me` endpoint in `app/routers/me.py`:
> 1. Is the upsert pattern correct for handling concurrent first-time requests?
> 2. Am I leaking PII in logs (I log clerk_user_id and email — too much)?
> 3. Should I use SQLAlchemy ORM objects vs raw SQL with result.mappings()?
> 4. Is the Pydantic response schema strict enough? Anything I should hide from the client?
> 5. Should this be 200 or 201 on first-call create? (I returned 200 — discuss tradeoffs.)

</details>

---

**When this issue is closed AND the app is deployed to Fly (#13), Phase 1 is functionally complete.** Take a screencast of the working endpoint. Recruiting artifact.
