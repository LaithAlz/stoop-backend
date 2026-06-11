---
title: "feat(backend): auth.users → landlords lifecycle sync via Postgres trigger"
labels: ["phase-1", "type-implementation", "auth", "size-s", "stretch"]
milestone: "Milestone 1: Walking skeleton"
---

> **Rewritten 2026-06-11** — was "Clerk webhook for user lifecycle events"
> (ADR-1). Because identity and app data now share one Postgres, the entire
> Svix-webhook design collapses into a database trigger. Effort drops M → S.

## Goal

Sync `auth.users` lifecycle (created / email updated / deleted) into the
`landlords` table with a Postgres trigger, so the app reacts to sign-ups,
email changes, and account deletions without polling or webhooks.

**Still marked stretch.** Lazy upsert in `/v1/me` (#11) covers the common
case. Add this when you need deletion handling or proactive provisioning.

## Why this matters

Lazy sync misses: email changes while logged out, account deletions, and
proactive provisioning. With Supabase, identity rows live in the same
database as app rows — a trigger is simpler, transactional, and has no
signature verification, retries, or idempotency bookkeeping to get wrong.

## Acceptance criteria

- [ ] Migration adds a `SECURITY DEFINER` function + triggers on `auth.users`:
  - [ ] `AFTER INSERT` → upsert `landlords` row (`auth_user_id`, `email`, `full_name` from `raw_user_meta_data`)
  - [ ] `AFTER UPDATE OF email` → update matching landlord's email
  - [ ] `AFTER DELETE` (or `deleted_at` update) → soft-delete landlord (`deleted_at = now()`), never hard-delete
- [ ] Trigger function is idempotent (upsert semantics — re-fire applies cleanly)
- [ ] Lazy upsert in `/v1/me` retained as the safety net (they complement)
- [ ] Isolation: function owned by a role that can write `landlords` but is not the app's request role
- [ ] Tests (against local Supabase / docker Postgres): insert auth user → landlord appears; update email → propagates; delete → soft-deleted
- [ ] `deleted_at` column migration included here if not already present

## Out of scope

- Hard-delete / data purge after grace period — Milestone 2+ scheduled job
- Reacting to other auth events (password change, MFA) — not our concern
- Auth Hooks (Supabase's HTTP hooks) — overkill for same-DB sync

## Effort & dependencies

- **Effort:** S (2–3 hours)
- **Blocks:** nothing critical for Milestone 1
- **Blocked by:** #8 (landlords table), #11

---

<details>
<summary><b>Hints</b></summary>

- Canonical Supabase pattern: `create function public.handle_new_user() returns trigger language plpgsql security definer set search_path = public as $$ begin insert into public.landlords (auth_user_id, email, full_name) values (new.id, new.email, new.raw_user_meta_data->>'full_name') on conflict (auth_user_id) do update set email = excluded.email; return new; end $$;` then `create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();`
- Manage this in an Alembic migration like everything else — don't click it into the dashboard and forget it exists.
- Supabase sometimes *updates* `auth.users.deleted_at` instead of deleting the row depending on deletion path — handle both.

</details>

<details>
<summary><b>Common gotchas</b></summary>

- `SECURITY DEFINER` + explicit `set search_path` — without the search_path pin this is a privilege-escalation foot-gun.
- A trigger failure on `auth.users` insert can **block sign-up entirely**. Keep the function body trivial and exception-safe (`exception when others then return new` is defensible here — lazy sync in #11 catches anything missed; log to a side table if you want visibility).
- Don't cascade-delete landlord data when auth user is deleted — soft-delete only; messages/audit_log are append-only and must survive for dispute records.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my auth lifecycle trigger migration:
> 1. Can a trigger failure block user sign-up? Have I made the function exception-safe?
> 2. Is SECURITY DEFINER scoped correctly (owner, search_path)?
> 3. Is soft-delete propagation consistent with the append-only audit_log policy?

</details>
