# Schema v1 — the single source of truth for names

> **Status:** Designed 2026-06-11. This is the #17 deliverable. Alembic
> migrations (#18, #19, #21, #24) implement exactly this — **column names
> here are canonical**; any agent or human writing code uses these names,
> never invents variants. Changes to this doc are schema changes.
> Conventions: `uuid` PKs (`gen_random_uuid()`), `timestamptz` everywhere,
> **text + CHECK instead of Postgres enums** (cheaper to evolve in Alembic),
> `landlord_id` on every multi-tenant table (the RLS key, policies in M-#22),
> soft deletes only where noted. Append-only tables enforced by REVOKE.
>
> **v1.1 amendments (2026-07-04)** — migration 0003 implements these,
> **pending** (#151):
> 1. New append-only table `message_status_events` — Twilio
>    delivery-status callbacks append here instead of ever touching
>    `messages`.
> 2. `messages.twilio_status` deprecated — superseded by
>    `message_status_events`; column stays listed below (migration 0002
>    already shipped it) until its DROP in migration 0003.
> 3. `messages.party` CHECK extended to
>    `('tenant','vendor','landlord')` for approve-by-SMS (#122)
>    command-channel replies. Deployed migration 0002 shipped the
>    narrower `CHECK (party IN ('tenant','vendor'))`; 0003 relaxes it to
>    the version shown below.
>
> **v1.2 amendments (2026-07-05)** — migration 0005 implements these (#22),
> the M2 isolation mechanism referenced by the `landlord_id` conventions
> note above. Live role facts verified against Supabase during the safety
> review: `postgres` and `service_role` are NOT superusers but DO hold
> `rolbypassrls = TRUE`; `authenticated`, `anon`, `authenticator` all have
> `rolbypassrls = FALSE`.
> 1. New Postgres role `app_role` (`NOLOGIN`) — created by the migration
>    with **no password, ever**, followed by a defensive unconditional
>    `ALTER ROLE app_role NOLOGIN` (guards a stale LOGIN-enabled role of
>    the same name surviving a re-migration). A human operator sets a
>    password later, once, directly against the target database
>    (`ALTER ROLE app_role LOGIN PASSWORD '...'`), only right before the
>    `APP_DATABASE_URL` Fly secret is set and only BEFORE any tenant data
>    exists (see `app/db/session.py`'s module docstring for the full
>    request-engine design this enables). `app/config.py` refuses to boot
>    at all when `ENVIRONMENT=production` and `APP_DATABASE_URL` is unset —
>    this step cannot be silently skipped in production.
> 2. Row-Level Security — `ENABLE ROW LEVEL SECURITY` ONLY (no `FORCE`) on
>    **every** table in this document except `alembic_version`, one
>    `FOR ALL TO app_role` policy per table, keyed off
>    `current_setting('app.current_landlord_id', true)::uuid` (the
>    `true` is `missing_ok`, so an unset session variable reads back as
>    SQL `NULL` — zero rows visible, zero rows writable; fail closed):
>    - Direct `landlord_id` match: `properties`, `vendors`, `tenants`,
>      `cases`, `messages`, `drafts`, `trust_metrics`, `audit_log`,
>      `notifications`, `push_tokens`.
>    - `landlords` itself: keyed on `id`, not `landlord_id` (it has none).
>    - `message_cases` (no `landlord_id`): `EXISTS` join through
>      `cases.id = message_cases.case_id`.
>    - `message_status_events` (no `landlord_id`, v1.1): `EXISTS` join
>      through `messages.id = message_status_events.message_id`.
>
>    **Why no `FORCE`:** `FORCE` only changes whether the TABLE OWNER
>    (the migrating/admin role) is also subject to RLS — it does nothing
>    for `app_role`, which was never the owner of anything and is fully
>    subject to RLS the moment `ENABLE` runs. The owner/admin path is a
>    DELIBERATE service path: `GET /v1/me`'s provisioning upsert
>    (`get_admin_session`), the migration-0004 auth trigger, and future
>    webhook ingestion (#40) all need to write unscoped by any GUC. `FORCE`
>    would have bound that path to RLS too — a no-op today given
>    `postgres`/`service_role`'s `rolbypassrls = TRUE`, but fragile: in any
>    environment where the owner lacked that attribute, `FORCE` would
>    silently swallow the auth-lifecycle trigger's writes (its own
>    exception handler treats an RLS violation like any other error and
>    swallows it, per its "never block sign-up" contract) — a silent
>    sign-up lockout with no error surfaced anywhere. Dropping `FORCE`
>    removes this failure class categorically instead of depending on a
>    role attribute this migration doesn't control.
> 3. Append-only enforcement (rule #2) actually lands:
>    `REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events
>    FROM app_role` — migrations 0002/0003 each documented this as
>    deferred to #22; this is that closure. `app_role` otherwise gets
>    ordinary `SELECT/INSERT/UPDATE/DELETE` on every other table, `USAGE`
>    on schema `public`, and `USAGE` on the two identity-column sequences
>    (`audit_log.id`, `message_status_events.id`).
> 4. Belt-and-braces against the Supabase Data API bypass channel, two
>    layers (both guarded — those PostgREST roles exist only on live
>    Supabase, silently skipped locally):
>    - `REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated`
>      — closes every table that exists right now.
>    - `ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES
>      FROM anon, authenticated` — closes every table the MIGRATING ROLE
>      creates in `public` in the future (every later migration runs as
>      that same role). **Standing note:** this does NOT cover a table in
>      a different schema, or one created by a different role (e.g. via
>      Supabase Studio) — any new schema or differently-owned table needs
>      the same explicit treatment; it is not automatically inherited.
>    The Data API should ALSO be disabled in the Supabase dashboard — a
>    human step, not something a migration can reach.
> 5. LangGraph checkpoint tables (`AsyncPostgresSaver.setup()`, #24) don't
>    exist yet — they will live in a dedicated schema reached only via the
>    admin engine, never `app_role`; their isolation lands with the graph
>    work itself, not with this migration.
> 6. `alembic_version` carries no RLS (migrations-only); the local-only
>    `auth.users` shim (migration 0004, #15) is untouched.
> 7. **Forward note for #40 (Twilio webhook ingestion):** the write path
>    that persists inbound messages MUST use the admin engine
>    (`get_admin_session`), never an RLS-scoped session — if landlord/
>    property resolution fails or races, an RLS-scoped session would
>    silently reject or misfile the INSERT instead of storing it, exactly
>    the catastrophic direction never-break rule #1 (the emergency line is
>    never gated) forbids. See `app/db/session.py`'s module docstring.
>
> **v1.3 amendments (2026-07-05)** — migration 0006 implements this
> (consolidated safety review, #40/#152, item 1 — a cross-process
> concurrency hole, reproduced 3/3 with genuinely overlapping
> transactions): an application-level `WHERE NOT EXISTS` check-then-insert
> is NOT safe across processes/connections — two truly concurrent webhook
> redeliveries of the same Twilio `MessageSid` can each pass the existence
> check before either commits its `INSERT`, so both insert an
> `emergency_call`/`needs_eyes` notification (duplicate escalations,
> unbounded under a replay storm). The only cross-process-safe fix is a
> real Postgres unique constraint the database itself enforces:
> 1. New partial unique expression index on `notifications`:
>    ```sql
>    CREATE UNIQUE INDEX uq_notifications_message_dedupe
>      ON notifications ((payload ->> 'message_id'), type)
>      WHERE type IN ('emergency_call', 'needs_eyes');
>    ```
>    Partial + expression: only `emergency_call`/`needs_eyes` rows are
>    covered by the uniqueness constraint — every other `type`
>    (`emergency_sms`, `draft_ready`, `recap`) is unaffected and may repeat
>    freely, exactly as before. A row whose `payload` has no `message_id`
>    key extracts SQL `NULL` via `->>`; ordinary SQL `NULL` semantics mean
>    Postgres unique indexes never treat two `NULL`s as equal, so any
>    number of such rows coexist without colliding — the dedupe key only
>    ever constrains rows that actually carry a `message_id` (every
>    `emergency_call`/`needs_eyes` row the webhook handler writes today).
>    The webhook's own `INSERT` switches from an application-level
>    existence check to `ON CONFLICT ((payload ->> 'message_id'), type)
>    WHERE type IN ('emergency_call', 'needs_eyes') DO NOTHING RETURNING
>    id` — Postgres's own conflict detection at the index level, safe
>    across arbitrarily many concurrent connections.
> 2. **Durability note:** `emergency_call`/`needs_eyes` `notifications`
>    rows are the durable idempotency anchor the webhook's `ON CONFLICT`
>    inference depends on — they must NEVER be deleted. Any future
>    retention/archival job must exclude `notifications` rows of these two
>    types (or exclude `notifications` entirely) from deletion; deleting
>    one would silently reopen the exact duplicate-escalation hole this
>    migration closes (a redelivered `MessageSid` would no longer find a
>    conflicting row and would re-fire the emergency protocol).
>
> **v1.4 amendments (2026-07-05)** — migration 0007 implements this (#24),
> closing the forward note in the v1.2 amendments block's point 5 and
> migration 0005's module docstring point 5: LangGraph's checkpoint tables
> now have a home.
> 1. New schema `langgraph` — `CREATE SCHEMA IF NOT EXISTS langgraph`, with
>    defensive `REVOKE ALL ... FROM PUBLIC` / `app_role` / (guarded)
>    `anon`/`authenticated`, mirroring migration 0005's Supabase Data API
>    belt-and-braces closure for `public`. This is an ADMIN-ENGINE-ONLY,
>    **RLS-free by construction** zone — `app_role` gets no grant on it at
>    all (not even `USAGE`), so there is no RLS policy to write and no
>    `public`-schema table for
>    `tests/test_rls_isolation_matrix.py::test_no_tables_outside_
>    descriptor_set_exist_in_public_schema` to ever see (that test only
>    scans `public`) — option (b) from that test's own docstring, chosen
>    deliberately over adding a 14th `TableDescriptor` + RLS policy.
> 2. No tables are created by the migration itself.
>    `AsyncPostgresSaver.setup()` (`langgraph-checkpoint-postgres`, called
>    idempotently by `app/agent/checkpointer.py`'s `setup_checkpointer()`
>    at FastAPI startup — `app/main.py`'s lifespan, after the #22 role-
>    separation self-check) creates and migrates its own four unqualified
>    tables — `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
>    `checkpoint_migrations` — the first time it runs against a connection
>    whose `search_path` is pinned to `langgraph` (see that module's
>    docstring for exactly how the pin is applied; the library itself has
>    no schema-qualification option, only unqualified table names in its
>    own SQL).
> 3. `app/agent/checkpointer.py` reaches this schema through its OWN
>    dedicated psycopg3 connection pool, built directly from
>    `settings.database_url` (the admin/service-role connection string) —
>    NEVER through `get_admin_session`/SQLAlchemy (a different driver/
>    connection path entirely) and NEVER through `app_role`/
>    `app_database_url`. No change to `get_admin_session`'s allowlist
>    (`tests/test_migrations_0005.py`) is needed: this module never
>    references that function.
> 4. Thread convention: ONE checkpoint thread per **case**, keyed on
>    `cases.langgraph_thread_id` (already `UNIQUE NOT NULL` since migration
>    0002) — never per tenant channel/phone number. Every graph invocation
>    (#25 onward) passes `{"configurable": {"thread_id":
>    case.langgraph_thread_id}}` as its `RunnableConfig`
>    (`docs/02-product/conversation-model.md`: a tenant's one SMS thread
>    maps to potentially many cases over time, each with its own
>    checkpoint history).

```sql
-- ───────────────────────── landlords ─────────────────────────
CREATE TABLE landlords (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_user_id        uuid NOT NULL UNIQUE,          -- supabase auth.users.id (JWT sub)
  email               text NOT NULL,
  full_name           text,
  phone               text,                          -- E.164; emergency calls go here
  timezone            text NOT NULL DEFAULT 'America/Toronto',
  voice_profile       jsonb,                         -- {tone: text, samples: text[]}
  price_cohort        text NOT NULL DEFAULT 'early_access'
                      CHECK (price_cohort IN ('early_access','standard')),
  subscription_tier   text NOT NULL DEFAULT 'free'
                      CHECK (subscription_tier IN ('free','full','desk')),
  subscription_status text NOT NULL DEFAULT 'none'
                      CHECK (subscription_status IN ('none','active','past_due','canceled')),
  stripe_customer_id  text UNIQUE,
  deleted_at          timestamptz,                   -- soft delete (auth trigger #15)
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── properties ────────────────────────
CREATE TABLE properties (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  label           text NOT NULL,                     -- "41 Palmerston"
  address_line1   text NOT NULL,
  city            text NOT NULL,
  province        text NOT NULL DEFAULT 'ON',
  postal_code     text,
  lat             double precision,                  -- for weather lookup (#30)
  lon             double precision,
  twilio_number   text UNIQUE,                       -- E.164; null until provisioned
  twilio_sid      text,
  house_rules     text,                              -- agent context, verbatim
  quiet_hours     jsonb NOT NULL DEFAULT '{"start":"21:00","end":"08:00"}',
  heating_season  jsonb NOT NULL DEFAULT '{"start":"09-15","end":"06-01"}',
  backup_contact  jsonb,                             -- {name, phone} for escalation T+10m
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_properties_landlord ON properties (landlord_id);
CREATE INDEX idx_properties_twilio   ON properties (twilio_number);

-- ───────────────────────── vendors ───────────────────────────
CREATE TABLE vendors (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id   uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  name          text NOT NULL,
  trade         text NOT NULL
                CHECK (trade IN ('plumbing','electrical','hvac','appliance',
                                 'locksmith','pest','general','other')),
  phone         text NOT NULL,                       -- E.164
  notes         text,                                -- "no Sundays; cash for <$100"
  working_hours jsonb,                               -- {mon:[["08:00","17:00"]],...}
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (landlord_id, phone)
);
CREATE INDEX idx_vendors_landlord ON vendors (landlord_id);

-- ───────────────────────── tenants ───────────────────────────
CREATE TABLE tenants (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id         uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  property_id         uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
  name                text,
  phone               text NOT NULL,                 -- E.164; the channel key
  unit                text,
  vulnerable_occupant text
                      CHECK (vulnerable_occupant IN ('infant','elderly','medical_device')),
  notes               text,
  active              boolean NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (property_id, phone)
);
CREATE INDEX idx_tenants_phone    ON tenants (phone);     -- inbound lookup hot path
CREATE INDEX idx_tenants_landlord ON tenants (landlord_id);

-- ───────────────────────── cases ─────────────────────────────
-- One issue, one severity, one LangGraph thread (conversation-model.md)
CREATE TABLE cases (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id         uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  property_id         uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  vendor_id           uuid REFERENCES vendors(id),   -- set when a vendor is engaged (#115)
  status              text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','awaiting_approval','awaiting_tenant',
                                        'resolved','reopened')),
  resolved_reason     text
                      CHECK (resolved_reason IN ('landlord','tenant_confirmed','auto_stale')),
  severity            text
                      CHECK (severity IN ('emergency','urgent','routine')),
  intent              text,                          -- maintenance|admin|question|other
  title               text,                          -- short agent-written summary
  langgraph_thread_id text UNIQUE NOT NULL,
  related_case_id     uuid REFERENCES cases(id),     -- >30d reopen → new case, linked
  emergency_fired_at  timestamptz,                   -- dedupe: protocol fires once per case
  last_activity_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_cases_queue    ON cases (landlord_id, status, severity);
CREATE INDEX idx_cases_tenant   ON cases (tenant_id, status);
CREATE INDEX idx_cases_activity ON cases (status, last_activity_at);  -- auto-stale sweep

-- ───────────────────────── messages (APPEND-ONLY) ────────────
CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
  tenant_id       uuid REFERENCES tenants(id),       -- null for vendor messages
  vendor_id       uuid REFERENCES vendors(id),       -- null for tenant messages
  case_id         uuid REFERENCES cases(id),         -- primary case; null = chitchat/pre-routing
  direction       text NOT NULL CHECK (direction IN ('inbound','outbound')),
  party           text NOT NULL CHECK (party IN ('tenant','vendor','landlord')),
                                                     -- 'landlord' added v1.1: approve-by-SMS
                                                     --  replies (#122) arrive as inbound SMS
                                                     --  and must be representable; landlord
                                                     --  rows are command-channel messages —
                                                     --  never forwarded to tenants/vendors,
                                                     --  excluded from tenant-conversation queries.
                                                     --  Landlord rows carry tenant_id/vendor_id
                                                     --  NULL (structural exclusion from channel
                                                     --  queries — the channel index is on
                                                     --  tenant_id), property_id = the property
                                                     --  whose number received the reply,
                                                     --  case_id = the referenced draft's case
  body            text NOT NULL,
  media           jsonb,                             -- [{url, content_type}] (#46)
  twilio_sid      text UNIQUE,                       -- idempotency key for webhooks
  twilio_status   text,                              -- DEPRECATED v1.1: never written after
                                                     --  insert; delivery state lives in
                                                     --  message_status_events; DROP scheduled
                                                     --  in migration 0003
  prefilter       jsonb,                             -- PrefilterResult snapshot (#107)
  classification  jsonb,                             -- {severity, rules_fired, modifier,
                                                     --  refusal_flags, reasoning}
  tokens_in       integer,
  tokens_out      integer,
  model           text,
  llm_cost_cents  numeric(10,4),
  sms_cost_cents  numeric(10,4),
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_messages_case    ON messages (case_id, created_at);
CREATE INDEX idx_messages_channel ON messages (tenant_id, created_at);
-- append-only: in migration, REVOKE UPDATE, DELETE ON messages FROM app_role;

CREATE TABLE message_cases (                          -- multi-issue messages
  message_id uuid NOT NULL REFERENCES messages(id),
  case_id    uuid NOT NULL REFERENCES cases(id),
  PRIMARY KEY (message_id, case_id)
);

-- ────────────── message_status_events (APPEND-ONLY, v1.1) ────
-- Twilio delivery-status callbacks append here. Delivery state is
-- derived by strict status precedence:
--   failed/undelivered > delivered > sent > sending > queued/accepted
-- (terminal states win; between terminals the failure wins so a real
-- failure is never masked); recency is NEVER the criterion (Twilio
-- repeats and reorders callbacks; a late transient row must not
-- regress a terminal state). Duplicates are appended as
-- facts — this is an event log, there is deliberately no UNIQUE
-- constraint and no upsert. This table exists because `messages` is
-- append-only (rule #2) — delivery status must never require an
-- UPDATE on messages.
CREATE TABLE message_status_events (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id  uuid NOT NULL REFERENCES messages(id),
  status      text NOT NULL CHECK (status IN ('accepted','queued','sending','sent','delivered','undelivered','failed')),
  error_code  text,
  payload     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_message_status_events_message ON message_status_events (message_id, created_at);
-- append-only: REVOKE UPDATE, DELETE ON message_status_events FROM app_role;

-- ───────────────────────── drafts ────────────────────────────
CREATE TABLE drafts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id       uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  case_id           uuid NOT NULL REFERENCES cases(id) ON DELETE RESTRICT,
  recipient         text NOT NULL CHECK (recipient IN ('tenant','vendor')),
  body              text NOT NULL,
  prompt_version    text NOT NULL,                   -- 'v1'
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','stale','approved','sending',
                                      'sent','rejected','cancelled')),
  auto_send         boolean NOT NULL DEFAULT false,  -- true only via trust ladder (#60)
  scheduled_send_at timestamptz,                     -- approve + 5s undo window
                                                     --  (#44; SMS approvals +5min, #122)
  sent_message_id   uuid REFERENCES messages(id),
  edited            boolean NOT NULL DEFAULT false,
  final_body        text,                            -- body actually sent if edited
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
-- one pending draft per case, ever (conversation-model.md invariant):
CREATE UNIQUE INDEX uq_drafts_one_pending ON drafts (case_id) WHERE status = 'pending';
CREATE INDEX idx_drafts_queue ON drafts (landlord_id, status);

-- ───────────────────────── trust_metrics ─────────────────────
CREATE TABLE trust_metrics (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id       uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  property_id       uuid NOT NULL REFERENCES properties(id) ON DELETE RESTRICT,
  severity          text NOT NULL CHECK (severity IN ('emergency','urgent','routine')),
  clean_approvals   integer NOT NULL DEFAULT 0,
  edited_approvals  integer NOT NULL DEFAULT 0,
  rejections        integer NOT NULL DEFAULT 0,
  consecutive_clean integer NOT NULL DEFAULT 0,      -- the graduation counter (#60)
  autonomy_unlocked boolean NOT NULL DEFAULT false,  -- only ever true for routine in v1
  unlocked_at       timestamptz,
  revoked_at        timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (property_id, severity)
);

-- ───────────────────────── audit_log (APPEND-ONLY) ───────────
CREATE TABLE audit_log (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  landlord_id uuid NOT NULL,
  case_id     uuid,
  actor       text NOT NULL CHECK (actor IN ('agent','landlord','system','prefilter')),
  action      text NOT NULL CHECK (action IN (
                'message_received','classified','case_opened','case_reopened',
                'case_resolved','drafted','draft_stale','approved','edited',
                'rejected','sent','send_cancelled','auto_sent',
                'emergency_triggered','emergency_call_attempt','acknowledged',
                'vendor_engaged','degraded_mode','trust_unlocked','trust_revoked',
                'billing_changed','settings_changed')),
  payload     jsonb NOT NULL DEFAULT '{}',           -- incl. rules_fired for classified
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_case     ON audit_log (case_id, created_at);
CREATE INDEX idx_audit_landlord ON audit_log (landlord_id, created_at);
-- append-only: REVOKE UPDATE, DELETE ON audit_log FROM app_role;

-- ───────────────────────── notifications ─────────────────────
-- Drives the emergency escalation chain state machine (#108)
CREATE TABLE notifications (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE RESTRICT,
  case_id         uuid REFERENCES cases(id),
  type            text NOT NULL CHECK (type IN ('emergency_call','emergency_sms',
                    'needs_eyes','draft_ready','recap')),
  channel         text NOT NULL CHECK (channel IN ('voice','sms','push','email')),
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','sent','acknowledged','failed','exhausted')),
  attempt         integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,                       -- the 60s sweeper key
  acknowledged_at timestamptz,                       -- stops the chain; the SLA metric
  payload         jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_sweep ON notifications (status, next_attempt_at);
-- v1.3 (migration 0006): cross-process-safe idempotency for the Twilio
-- webhook's emergency_call/needs_eyes artifact creation (ON CONFLICT
-- target) -- see the v1.3 amendments note above for the full rationale.
-- NEVER deleted: notifications of these two types anchor this dedupe.
CREATE UNIQUE INDEX uq_notifications_message_dedupe
  ON notifications ((payload ->> 'message_id'), type)
  WHERE type IN ('emergency_call', 'needs_eyes');

-- ───────────────────────── push_tokens ───────────────────────
CREATE TABLE push_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id  uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
  token        text NOT NULL UNIQUE,
  platform     text NOT NULL CHECK (platform IN ('ios','android','web')),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- LangGraph checkpoint tables: created by AsyncPostgresSaver.setup() (#24),
-- service-role connection, thread_id = cases.langgraph_thread_id. They live
-- in the dedicated `langgraph` schema (migration 0007), NOT here in
-- `public` -- see the v1.4 amendments block above.
```

## Notes for implementers (human or agent)

- **Never invent a column.** If a need isn't covered here, the schema doc
  changes first (one commit), then the migration.
- `text + CHECK` over Postgres enums: adding a value is an
  `ALTER ... DROP/ADD CONSTRAINT`, not an enum migration dance.
- Append-only enforcement is part of the migration, not a convention:
  `REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events
  FROM <app role>` — implemented by migration 0005 (`app_role`, v1.2
  amendments above).
- The undo window is data, not a sleep: dashboard approve sets
  `drafts.scheduled_send_at = now() + 5s` (#44); approve-by-SMS sets
  `now() + 5 minutes` (#122, per `plain-language-rules.md` — SMS has no
  undo bar). Same mechanism either way: the sender only sends rows whose
  time has come and whose status is still `approved`.
- RLS (#22, migration 0005) keys every policy off `landlord_id` (or,
  where a table has none, an `EXISTS` join to one that does — see the
  v1.2 amendments above) matched to
  `current_setting('app.current_landlord_id', true)::uuid`, `TO app_role`.
  `require_landlord` (`app/deps.py`) is what actually sets that session
  variable, per request, via `set_config(..., true)`.
- Money columns are `numeric` cents, never floats.
