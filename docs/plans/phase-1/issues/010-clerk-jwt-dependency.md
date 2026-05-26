---
title: "feat(backend): Clerk JWT verification dependency"
labels: ["phase-1", "type-implementation", "auth", "size-m"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Build a FastAPI dependency that verifies an incoming Clerk JWT and returns the authenticated user's Clerk identity (`user_id`, `email`, name).

## Why this matters

This is the gatekeeper for every authenticated endpoint. Get it right once; trust it forever.

## Acceptance criteria

- [ ] `app/integrations/clerk.py` exposes a JWT verification function
- [ ] Verification uses JWKS (public-key cryptography), NOT the secret key
- [ ] JWKS fetched once and cached (24h TTL is fine)
- [ ] `app/deps.py` exposes a `require_clerk_user` FastAPI dependency
- [ ] Returns a typed object with `user_id`, `email`, `first_name`, `last_name`
- [ ] Invalid JWT (bad signature) → 401 with structured error body
- [ ] Expired JWT → 401 with `expired` error code
- [ ] Missing `Authorization` header → 401
- [ ] Malformed `Authorization` (no Bearer prefix) → 401
- [ ] Valid JWT → returns the user object correctly
- [ ] Temporary test endpoint (`/v1/auth-test`) returns user info — use for manual verification with real Clerk JWTs

## Out of scope

- Don't build the get-or-create landlord logic — issue #11
- Don't set up Clerk webhooks — issue #15
- Don't store JWTs in the database (stateless verification only)
- Don't implement role-based access — Phase 5+

## Effort & dependencies

- **Effort:** M (4-6 hours, JWT details deserve care)
- **Blocks:** #11, #15
- **Blocked by:** #4, #6

---

<details>
<summary><b>Design this first — don't outsource the JWT design</b></summary>

This is one of the highest-stakes pieces of Phase 1. Understand it deeply.

Read up first:
1. JWT structure: header (alg, kid), payload (claims), signature
2. Asymmetric vs symmetric signing — Clerk uses RS256 (asymmetric). The "secret key" is for backend→Clerk API calls; **JWT verification uses the public key from JWKS**.
3. Why JWKS not secret key? Public-key verification means compromising your backend doesn't let attackers forge new JWTs.

Sketch the flow on paper:
1. Extract `Authorization: Bearer <token>` header
2. Decode JWT header (no verification yet) to get the `kid` (key ID)
3. Fetch JWKS, find the matching public key by `kid`
4. Verify signature, expiration, issuer
5. Return the claims as a typed object

Design the user object: which Clerk claims do you care about?

</details>

<details>
<summary><b>Hints</b></summary>

- Use `pyjwt[crypto]` for JWT verification — handles RS256 with the `cryptography` library
- `pyjwt.algorithms.RSAAlgorithm.from_jwk(jwk_dict)` converts a JWKS entry into a usable verification key
- Cache JWKS in a module-level variable with a TTL. Concurrent fetches are a minor concern (race condition just causes a duplicate fetch, not incorrect behavior)
- Use a dataclass (frozen) or Pydantic model for the returned user object — typed and immutable
- For the FastAPI dependency, use `Header(None)` to accept optional Authorization, then raise HTTPException(401) if missing
- Define a custom exception (`ClerkAuthError`) with an error code so HTTP layer can structure the response

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Don't verify with the secret key.** Clerk's docs may show that pattern for older API versions — it works but is the wrong primitive. JWKS is correct.
- **The `alg: none` attack.** Don't accept tokens with `alg: none`. PyJWT's `algorithms=["RS256"]` whitelist prevents this.
- **Don't log JWTs.** Anywhere. Ever. Even in DEBUG.
- **`verify_aud=False` is often necessary** for Clerk because they don't always set the audience claim. Audience verification is optional security; skip it if Clerk's templates don't include it consistently.
- **Clerk JWTs are short-lived** (~60 seconds by default). For manual testing, you'll re-sign-in often, or configure a long-lived testing token in Clerk's JWT templates.
- The `kid` (key ID) is in the JWT *header*, not the payload. Use `pyjwt.get_unverified_header(token)` to read it before verification.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my Clerk JWT verification in `app/integrations/clerk.py` and `app/deps.py`. Focus on:
> 1. Is JWKS caching safe under concurrent requests?
> 2. Am I verifying all claims I should? (`exp`, `iss`, `nbf` — what about `azp`?)
> 3. Any bypass vectors (alg: none, JWT confusion, weak signature checks)?
> 4. Is `verify_aud=False` justified for Clerk, or should I configure an audience?
> 5. Is my error messaging leaking anything sensitive about what failed?

</details>
