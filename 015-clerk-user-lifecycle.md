---
title: "feat(backend): Clerk webhook for user lifecycle events"
labels: ["phase-1", "type-implementation", "auth", "size-m", "stretch"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Receive Clerk webhooks for `user.created`, `user.updated`, `user.deleted` and sync to the `landlords` table. Verify webhook signatures.

## Why this matters

Lazy sync on `/v1/me` (issue #11) covers the common case but misses: profile updates when the user isn't logged in, account deletions, and proactive provisioning. Webhooks are the cleaner long-term path.

**Marked stretch.** You can ship Phase 1 without this. Lazy create on `/v1/me` is enough. Add this when you need to react to user deletion or get into multi-device email-sync edge cases.

## Acceptance criteria

- [ ] `POST /webhooks/clerk` endpoint exists
- [ ] Endpoint verifies the Svix-signed webhook signature
- [ ] Invalid signature → 401, no DB write
- [ ] Handler responds within 5 seconds (Svix retries on timeout)
- [ ] `user.created` event → upserts a `landlords` row (idempotent — running twice doesn't duplicate)
- [ ] `user.updated` event → updates email/full_name on the matching landlord
- [ ] `user.deleted` event → soft-deletes the landlord (set `deleted_at`, don't hard-delete yet)
- [ ] Idempotency: receiving the same event id twice doesn't apply twice
- [ ] Webhook configured in Clerk dashboard pointing to your Fly deployment

## Out of scope

- Don't hard-delete user data on `user.deleted` — that comes after a 60-day grace period via a scheduled job (Phase 5)
- Don't try to handle every Clerk event type — just the three user lifecycle events
- Don't build webhook retry logic — Svix retries automatically

## Effort & dependencies

- **Effort:** M (4-6 hours)
- **Blocks:** Nothing critical for Phase 1
- **Blocked by:** #11, #13 (need deployed URL for Clerk to call)

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Idempotency.** Clerk may send the same event twice (especially during retries). Store processed `event_id`s in a small table and skip duplicates. Or use the upsert pattern from #11.

2. **Signature verification.** Clerk uses Svix for webhooks. Svix signs requests with a secret. Verify the signature before doing anything else.

3. **What about lazy sync?** Keep the lazy sync in `/v1/me` — webhooks are not 100% reliable, lazy sync is the safety net. They complement.

4. **Soft delete on `user.deleted`.** Don't drop the row immediately. Set a `deleted_at` timestamp. A scheduled job hard-deletes after 60 days. (You can add the `deleted_at` column in a quick migration as part of this issue, or do it in Phase 5.)

</details>

<details>
<summary><b>Hints</b></summary>

- Add the `svix` Python package: `uv add svix`
- Svix verifies via three headers: `svix-id`, `svix-timestamp`, `svix-signature`
- The webhook secret comes from Clerk's dashboard → Webhooks → Add Endpoint → Signing Secret
- Configure the endpoint in Clerk: URL is your Fly deployment + `/webhooks/clerk`, events are `user.created`, `user.updated`, `user.deleted`
- The event payload includes `type` (the event name) and `data` (the user object)
- For idempotency, the `svix-id` header is the unique event identifier — store these
- Return 204 No Content quickly. Don't do slow operations synchronously inside the webhook handler.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Don't skip signature verification.** Without it, anyone can spoof webhook events and create / update / delete users in your DB.
- **Don't process the payload before verifying.** The signature covers the raw body — read the body bytes, verify, then parse JSON.
- **Don't trust the IP allowlist instead of signatures.** Svix's IPs change.
- **Time skew.** Svix rejects timestamps older than 5 minutes. If your server clock drifts, signature verification fails mysteriously.
- **The `data` object structure** differs per event type. `user.created`'s data is a user object. `user.deleted`'s data may just be `{id, deleted: true}`. Read Clerk's webhook docs for current schemas.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my Clerk webhook handler in `app/routers/webhooks_clerk.py`:
> 1. Is the Svix signature verification correct? Any bypass vectors?
> 2. Am I handling idempotency well? What if the same event is delivered three times?
> 3. Is the soft-delete pattern correct (column exists in migration, deletion flow)?
> 4. Anything I should rate-limit on this endpoint?

</details>
