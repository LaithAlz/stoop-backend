---
title: "chore(infra): create Supabase project and document setup"
labels: ["phase-1", "type-setup", "infra", "size-xs"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Set up a Supabase project for development. Document how to connect from the backend.

## Why this matters

Need a real Postgres before any migration or query work. Supabase gives managed Postgres + Storage + a clean upgrade path.

## Acceptance criteria

- [ ] Supabase project `stoop-dev` created in **Canada (Central)** region
- [ ] Database password generated and stored in password manager (never in repo)
- [ ] You have the **transaction-mode pooler** connection string (port 6543, not direct 5432)
- [ ] You have the service-role key stored separately from the anon key
- [ ] `psql "$DATABASE_URL" -c "SELECT 1"` returns `1` from your local machine
- [ ] `gen_random_uuid()` works against the database
- [ ] `.env.example` in repo lists every Supabase env var with placeholder values
- [ ] `docs/setup/supabase.md` documents the process for future-you

## Out of scope

- Don't run any migrations yet — issue #8
- Don't set up Supabase Storage — Phase 4
- Don't create staging or prod project — issue #13 / Phase 9
- Supabase Auth IS our auth (ADR-1, replaced Clerk) — but configuring it is #4, not this issue

## Effort & dependencies

- **Effort:** XS (30-45 min)
- **Blocks:** #8, #9, #11
- **Blocked by:** None (parallel with #1, #2, #4)

---

<details>
<summary><b>Hints</b></summary>

- The pooler connection string lives under: Project → Settings → Database → Connection pooling. Mode: Transaction. The URL uses port 6543.
- API keys live under: Project → Settings → API. The "anon" key is for client SDKs (mobile/web); the "service_role" key bypasses RLS and is backend-only.
- `gen_random_uuid()` is built into Postgres 13+, no extension needed
- Ignore the legacy JWT secret (Settings → API → JWT Settings) — #4 enables asymmetric signing keys; the backend verifies via JWKS, never the shared secret

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Pooler vs direct.** Use the pooler (port 6543) from your backend. Direct connections (port 5432) work but get exhausted under any scaling. The pooler URL format is slightly different — it has `pooler.supabase.com` in the host.
- **Free tier sleeps.** After 1 week of no API requests the project pauses. Hit `/healthz` from your phone occasionally if you take a break, or upgrade to Pro ($25/mo) when ready.
- **Service-role key is dangerous.** It bypasses RLS entirely. Treat like a root password. Never log, never commit, never expose to client code.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> I just set up a Supabase project. Review my `.env.example` and `docs/setup/supabase.md`:
> 1. Am I using the right connection string format?
> 2. Is the anon-vs-service-role distinction clear?
> 3. Anything I should document about Supabase's quirks (free-tier sleep, etc.)?

</details>
