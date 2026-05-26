---
title: "feat(backend): Alembic setup and create_landlords migration"
labels: ["phase-1", "type-implementation", "database", "size-m"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Configure Alembic for async Postgres and write the first migration: the `landlords` table.

## Why this matters

First real schema work. The `landlords` table is the root of multi-tenancy. Getting Alembic right now means every future migration is straightforward.

## Acceptance criteria

- [ ] `alembic.ini` exists and reads connection URL from settings (not hardcoded)
- [ ] `migrations/env.py` configured for async SQLAlchemy migrations
- [ ] Migration creates `landlords` table with all fields per `backend-spec.html` §03 (clerk_user_id, email, phone, full_name, timezone, notification_prefs, stripe_customer_id, subscription_tier, subscription_status, trial_ends_at, created_at, updated_at)
- [ ] UNIQUE constraint on `clerk_user_id`
- [ ] UNIQUE constraint on `stripe_customer_id`
- [ ] CHECK constraints on `subscription_tier` and `subscription_status` for valid values
- [ ] Indexes on `clerk_user_id` and `stripe_customer_id`
- [ ] `alembic upgrade head` runs clean against the Supabase dev DB
- [ ] `alembic downgrade -1` cleanly drops the table
- [ ] `alembic upgrade head` re-applies cleanly (reversibility verified)
- [ ] Table visible in Supabase Dashboard

## Out of scope

- Don't add other tables (properties, conversations, etc.) — Phase 2
- Don't add RLS policies — Phase 2/5
- Don't seed any data
- Don't add SQLAlchemy ORM models yet (raw SQL in migration is fine) — they come in #9

## Effort & dependencies

- **Effort:** M (4-6 hours, mostly because async Alembic has fiddly setup)
- **Blocks:** #9, #10, #11
- **Blocked by:** #3, #6

---

<details>
<summary><b>Design this first — most important task in Phase 1</b></summary>

**Don't touch Alembic until you've designed the table on paper.**

Sketch the `landlords` table:
- **Columns.** What identity field? What profile fields? Where does notification preference data live (JSONB? structured columns? why)? What billing state fields?
- **Types.** Postgres types matter. `text` vs `varchar`, `timestamptz` vs `timestamp`, `jsonb` vs `json`.
- **Constraints.** NOT NULL where? CHECK constraints on which fields?
- **Indexes.** What's the hot query path? (Hint: every authenticated request looks up by `clerk_user_id`.)

Then open `backend-spec.html` §03 and diff. Where your design differs from mine, decide which is better. Some of yours will be better.

</details>

<details>
<summary><b>Hints</b></summary>

- Initialize Alembic with the async template: `alembic init -t async migrations`
- Remove `sqlalchemy.url` from `alembic.ini`. Set it dynamically in `env.py` from `settings.database_url`.
- asyncpg needs the URL scheme `postgresql+asyncpg://` — transform from `postgresql://` in `env.py`
- Phase 1 has no ORM models, so use raw SQL in the migration via `op.execute("CREATE TABLE ...")`. Don't fight Alembic's autogen when there's no metadata to compare against.
- Indexes go in separate `op.execute("CREATE INDEX ...")` statements after the table creation
- `gen_random_uuid()` is built into Postgres 13+ — no need for `uuid-ossp` extension

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't use Alembic's `op.create_table()` for this migration — it's awkward with the constraints we need. Raw `op.execute()` with the full CREATE TABLE statement is cleaner.
- `target_metadata = None` is correct for now (no ORM models yet). Update it once you have models.
- Migration files have an auto-generated `revision` ID at the top. Don't change it after committing.
- `op.drop_table()` works in downgrade, OR you can `op.execute("DROP TABLE IF EXISTS landlords")` — IF EXISTS is more idempotent.
- The pooler URL (port 6543) does work for migrations, but if you see weird "prepared statement already exists" errors, try the direct connection (port 5432) for migrations specifically — pgBouncer transaction mode doesn't love prepared statements.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my Alembic config (`migrations/env.py`) and the first migration:
> 1. Is the async migration setup correct?
> 2. Are the column types right?
> 3. Am I missing CHECK constraints I should have (e.g., email format)?
> 4. Are the indexes right for the queries Phase 1 needs?
> 5. Is the downgrade truly reversible?

</details>
