---
title: "chore(infra): configure Supabase Auth and obtain JWT verification keys"
labels: ["phase-1", "type-setup", "infra", "size-xs"]
milestone: "Milestone 1: Walking skeleton"
---

> **Rewritten 2026-06-11** — was "create Clerk application" (ADR-1 in
> `docs/03-engineering/architecture.md`). Same number so cross-references hold.

## Goal

Configure Auth in the existing Supabase project (the one from #3). Enable
providers, switch to asymmetric JWT signing keys, and capture everything #10
needs for JWKS verification.

## Why this matters

Supabase Auth handles all landlord authentication. One vendor instead of two
(Clerk cut): identity lives next to the data, and lifecycle sync becomes a
same-database trigger (#15) instead of signed webhooks.

## Acceptance criteria

- [ ] Auth configured in the Supabase project from #3 (no separate signup)
- [ ] Providers enabled: Email/Password (email confirmation required), Apple, Google
- [ ] **Asymmetric JWT signing keys enabled** (project settings → JWT keys) — do NOT build on the legacy shared HS256 secret
- [ ] JWKS URL noted: `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`
- [ ] Issuer noted: `https://<ref>.supabase.co/auth/v1`
- [ ] Test user created (dashboard or `POST /auth/v1/signup`)
- [ ] Obtained a real access token via `POST /auth/v1/token?grant_type=password` with the anon key
- [ ] Token decoded at jwt.io — claims structure verified (`sub`, `email`, `aud: "authenticated"`, `iss`)
- [ ] `.env.example` includes `SUPABASE_URL`, `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER` (service-role key already documented by #3)
- [ ] `docs/setup/supabase-auth.md` documents the setup

## Out of scope

- No backend code — that's #10
- No native Apple/Google flows — mobile is deferred
- No `auth.users` → `landlords` trigger — that's #15
- No production project — Milestone 3

## Effort & dependencies

- **Effort:** XS (30–45 min)
- **Blocks:** #10, #11, #15
- **Blocked by:** #3

---

<details>
<summary><b>Hints</b></summary>

- Getting a test token without any client app:
  `curl -X POST "https://<ref>.supabase.co/auth/v1/token?grant_type=password" -H "apikey: <anon-key>" -H "Content-Type: application/json" -d '{"email":"...","password":"..."}'`
  → `access_token` in the response.
- Supabase access tokens default to 1h expiry (much friendlier for manual testing than Clerk's ~60 s).
- The anon key is a *client* credential (like Clerk's publishable key). The service-role key is the backend superpower — never in the frontend, never committed.
- Email confirmation ON: the landlord's email is their billing identity.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Legacy projects sign JWTs with a shared HS256 secret.** Verifying with a shared secret means anyone with the secret can mint tokens. Enable the asymmetric signing keys feature and verify via JWKS — same primitive the old Clerk plan used.
- After enabling new signing keys, *old tokens signed with the legacy secret may still circulate* until expiry — sign out test users and mint fresh tokens.
- `aud` is `"authenticated"` for normal users — verify it (unlike Clerk, Supabase sets it consistently).
- The JWKS may rotate keys; #10 must match by `kid`, not take the first key.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> I set up Supabase Auth. Review `docs/setup/supabase-auth.md`:
> 1. Anything sensitive accidentally documented?
> 2. Are the enabled providers right for a landlord dashboard (web-first, mobile later)?
> 3. Anything about asymmetric keys / JWKS I should pin down now to avoid pain in #10?

</details>
