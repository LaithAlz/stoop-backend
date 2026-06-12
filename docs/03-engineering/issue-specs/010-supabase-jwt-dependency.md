---
title: "feat(backend): Supabase JWT verification dependency"
labels: ["phase-1", "type-implementation", "auth", "size-m"]
milestone: "Milestone 1: Walking skeleton"
---

> **Rewritten 2026-06-11** — was "Clerk JWT verification dependency" (ADR-1).
> The design is deliberately identical in shape; only the issuer changed.

## Goal

Build a FastAPI dependency that verifies an incoming Supabase access token
and returns the authenticated user's identity (`user_id`, `email`, name).

## Why this matters

This is the gatekeeper for every authenticated endpoint. Get it right once;
trust it forever.

## Acceptance criteria

- [ ] `app/integrations/supabase_auth.py` exposes a JWT verification function
- [ ] Verification uses JWKS (asymmetric, public-key), NOT the legacy shared secret
- [ ] JWKS fetched once and cached (24h TTL), key matched by `kid`
- [ ] Verifies signature, `exp`, `iss` (`https://<ref>.supabase.co/auth/v1`), and `aud` (`"authenticated"`)
- [ ] `app/deps.py` exposes a `require_user` FastAPI dependency
- [ ] Returns a typed, frozen object: `user_id` (UUID from `sub`), `email`, `full_name` (from `user_metadata`, may be None)
- [ ] Invalid signature → 401 structured error · expired → 401 `expired` code · missing/malformed `Authorization` → 401
- [ ] Valid JWT → returns the user object
- [ ] Temporary `/v1/auth-test` endpoint for manual verification with real tokens

## Out of scope

- Get-or-create landlord — #11
- `auth.users` → `landlords` trigger — #15
- Role-based access — Milestone 2+

## Effort & dependencies

- **Effort:** M (4–6 hours — JWT details deserve care)
- **Blocks:** #11, #15
- **Blocked by:** #4, #6

---

<details>
<summary><b>Design this first — don't outsource the JWT design</b></summary>

Same flow as any JWKS-verified JWT (this knowledge transfers to every auth
vendor):

1. Extract `Authorization: Bearer <token>`
2. `jwt.get_unverified_header(token)` → `kid`
3. Fetch JWKS (cached), select key by `kid`
4. Verify signature + `exp` + `iss` + `aud` with an explicit algorithm allowlist
5. Map claims → frozen identity object

Decide which claims you trust: `sub` is the stable UUID (FK target for
`landlords.auth_user_id`); `email` can change; `user_metadata.full_name`
is user-writable — treat as display-only, never as authorization input.

</details>

<details>
<summary><b>Hints</b></summary>

- `pyjwt[crypto]`; build keys with `jwt.PyJWKClient(jwks_url)` — it handles `kid` matching and caching, or roll the cache yourself with a module-level TTL.
- Supabase signing keys may be **ES256 (ECC), not RS256** — set `algorithms=["ES256", "RS256"]` from what the JWKS actually advertises; never accept `none`.
- `aud="authenticated"` — pass `audience="authenticated"` to `jwt.decode` (unlike Clerk, don't skip aud verification).
- Frozen dataclass or Pydantic model for the identity object.
- Custom `AuthError` with an error code so the HTTP layer structures the 401 body.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Don't verify with the legacy shared JWT secret.** Works, but it's a symmetric primitive — a leaked secret mints valid tokens. JWKS only.
- **The `alg: none` attack** — explicit algorithm allowlist, always.
- **Don't log JWTs.** Anywhere. Ever. Even in DEBUG.
- The service-role key also produces JWTs (`role: "service_role"`); your dependency must accept only `role: "authenticated"` user tokens — check the claim.
- `kid` lives in the JWT *header*, not the payload.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review `app/integrations/supabase_auth.py` and `app/deps.py`:
> 1. Is JWKS caching safe under concurrent requests?
> 2. Am I verifying everything I should (`exp`, `iss`, `aud`, `role`)?
> 3. Any bypass vectors (alg confusion, service-role tokens, weak allowlist)?
> 4. Is my error messaging leaking which check failed?

</details>
