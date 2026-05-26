---
title: "chore(infra): create Clerk application and obtain dev keys"
labels: ["phase-1", "type-setup", "infra", "size-xs"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Set up a Clerk application for development. Configure auth providers. Obtain the keys you'll need for JWT verification (issue #10).

## Why this matters

Clerk handles all landlord authentication. We need a real Clerk instance to test JWT verification and `/v1/me`.

## Acceptance criteria

- [ ] Clerk application `Stoop (dev)` created
- [ ] Auth providers enabled: Email/Password (with email verification required), Apple, Google
- [ ] Secret key stored in password manager — never committed
- [ ] Publishable key noted (for mobile app later)
- [ ] JWKS URL noted (backend uses this for signature verification)
- [ ] Test user created via Clerk dashboard
- [ ] Successfully signed in to the test user via Clerk's hosted sign-in page and obtained a JWT
- [ ] JWT decoded at jwt.io to verify claims structure
- [ ] `.env.example` includes Clerk env vars
- [ ] `docs/setup/clerk.md` documents the setup

## Out of scope

- Don't write any backend code — that's #10
- Don't configure Apple/Google native sign-in flows — that's mobile (Phase 7)
- Don't set up webhooks yet — that's #15
- Don't create a production Clerk app — Phase 9

## Effort & dependencies

- **Effort:** XS (30-45 min)
- **Blocks:** #10, #11, #15
- **Blocked by:** None (parallel with #1, #2, #3)

---

<details>
<summary><b>Hints</b></summary>

- Clerk's docs are good. The dashboard is at clerk.com → your application
- Required auth providers: Email/Password is the baseline. Apple and Google reduce mobile friction. Phone auth is off (Twilio numbers are for tenants, not landlords).
- Email verification required: ON. The landlord's email is their billing identity.
- To get a test JWT: use Clerk's hosted sign-in page at `https://<your-app>.accounts.dev/sign-in`, sign in, then either grab the `__session` cookie value from DevTools or use Clerk's CLI / Postman with the secret key
- JWKS URL format: `https://<your-app>.clerk.accounts.dev/.well-known/jwks.json`. You can also find it at Clerk Dashboard → API Keys → JWT Templates page

</details>

<details>
<summary><b>Common gotchas</b></summary>

- The "secret key" is for Clerk's backend API (creating users, sending magic links). The "publishable key" is for client SDKs. You'll use **the JWKS URL** to verify JWTs, not the secret key directly.
- Clerk JWTs are short-lived (~1 minute default). For testing, you'll keep re-signing in or use Clerk's long-lived testing tokens
- The audience (`aud`) claim is sometimes omitted by Clerk depending on the JWT template. Don't fail verification if `aud` is missing

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> I set up Clerk for the backend. Review my `docs/setup/clerk.md`:
> 1. Anything sensitive accidentally documented?
> 2. Are the auth providers I enabled right for a mobile-first landlord app?
> 3. Anything about JWT validation I should note now to avoid headaches in #10?

</details>
