---
title: "feat(backend): GET /v1/me endpoint with get-or-create landlord"
labels: ["phase-1", "type-implementation", "auth", "size-s", "gate"]
milestone: "Milestone 1: Walking skeleton"
---

> **Updated 2026-06-11** — Clerk references swapped for Supabase Auth
> (ADR-1). The pattern is unchanged.

## Goal

`GET /v1/me` returns the authenticated landlord's profile. First call lazily
creates the landlord record from the verified JWT claims; subsequent calls
return the same record.

**This is the deployment gate for the backend foundation.** When this works
in production (after #13), the foundation slice of Milestone 1 is done.

## Why this matters

The seam between Supabase Auth (identity) and our application tables. Every
future authenticated endpoint follows this pattern: verify JWT → load local
landlord → set RLS session variable (Milestone 2) → business logic.

## Acceptance criteria

- [ ] `GET /v1/me` returns the landlord profile with a valid Supabase access token
- [ ] First call for a new auth user → creates a `landlords` row keyed by `auth_user_id` (the JWT `sub` UUID) → returns profile
- [ ] Subsequent calls → same `landlords.id`, no duplicates
- [ ] Email changed in Supabase Auth → local record refreshed on next read (lazy sync)
- [ ] Typed Pydantic response: `id`, `email`, `full_name`, `timezone`, `subscription_tier`, `subscription_status`, `trial_ends_at`, `created_at`
- [ ] No `Authorization` header → 401 (from #10's dependency)
- [ ] Race-safe: concurrent first-time requests don't surface a duplicate-key error (upsert)
- [ ] Test: real token, two calls, same id · Test: no auth → 401

## Out of scope

- `PATCH /v1/me`, subscription management, avatar, notification prefs — later milestones

## Effort & dependencies

- **Effort:** S (3–4 hours)
- **Blocks:** closes the foundation gate when deployed (after #13)
- **Blocked by:** #8, #9, #10

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Get-or-create**: no separate sign-up endpoint — Supabase handles auth,
   we just need a row. (#15's DB trigger makes this proactive later; lazy
   upsert stays as the safety net.)
2. **Race condition**: two concurrent first calls both INSERT → use Postgres
   upsert: `INSERT … ON CONFLICT (auth_user_id) DO UPDATE … RETURNING *`.
3. **Response design**: what does the dashboard need? Don't dump the row.
4. **Email sync**: refresh local email from the verified claim on read.

</details>

<details>
<summary><b>Hints</b></summary>

- `INSERT INTO landlords (auth_user_id, email, full_name) VALUES (…) ON CONFLICT (auth_user_id) DO UPDATE SET email = EXCLUDED.email, full_name = COALESCE(EXCLUDED.full_name, landlords.full_name), updated_at = now() RETURNING *`
- `full_name` comes from `user_metadata` — user-writable, display-only.
- `response_model=MeResponse` for response validation.
- structlog: `bind_contextvars(auth_user_id=user.user_id)` — but see gotchas on PII.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- SELECT-then-INSERT is a race. Upsert.
- A GET with a lazy-create side effect is fine — document it; Stripe/GitHub do the same for "current user".
- Don't expose `auth_user_id` in the response — internal detail.
- Don't log emails; the `auth_user_id` UUID is enough correlation.
- The audit log exists from Milestone 1 in the new plan — but `/v1/me` reads don't belong in it; it's for agent/landlord actions on conversations.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review `app/routers/me.py`:
> 1. Is the upsert race-safe and idempotent?
> 2. PII in logs?
> 3. Response schema: anything leaking that the client doesn't need?
> 4. 200 vs 201 on first-call create — defensible either way; did I document the choice?

</details>

---

**When this is closed AND deployed to Fly (#13), the foundation gate is
passed.** Screencast it — recruiting artifact.
