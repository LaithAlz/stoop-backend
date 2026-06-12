# Schema v1 — the single source of truth for names

> **Status:** Designed 2026-06-11. This is the #17 deliverable. Alembic
> migrations (#18, #19, #21, #24) implement exactly this — **column names
> here are canonical**; any agent or human writing code uses these names,
> never invents variants. Changes to this doc are schema changes.
> Conventions: `uuid` PKs (`gen_random_uuid()`), `timestamptz` everywhere,
> **text + CHECK instead of Postgres enums** (cheaper to evolve in Alembic),
> `landlord_id` on every multi-tenant table (the RLS key, policies in M-#22),
> soft deletes only where noted. Append-only tables enforced by REVOKE.

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
  party           text NOT NULL CHECK (party IN ('tenant','vendor')),
  body            text NOT NULL,
  media           jsonb,                             -- [{url, content_type}] (#46)
  twilio_sid      text UNIQUE,                       -- idempotency key for webhooks
  twilio_status   text,
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
  scheduled_send_at timestamptz,                     -- approve + 5s undo window (#44)
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
-- service-role connection, thread_id = cases.langgraph_thread_id.
```

## Notes for implementers (human or agent)

- **Never invent a column.** If a need isn't covered here, the schema doc
  changes first (one commit), then the migration.
- `text + CHECK` over Postgres enums: adding a value is an
  `ALTER ... DROP/ADD CONSTRAINT`, not an enum migration dance.
- Append-only enforcement is part of the migration, not a convention:
  `REVOKE UPDATE, DELETE ON messages, audit_log FROM <app role>`.
- The undo window is data, not a sleep: approve sets
  `drafts.scheduled_send_at = now() + 5s`; the sender only sends rows whose
  time has come and whose status is still `approved`.
- RLS (#22) keys every policy off `landlord_id` matched to
  `current_setting('app.current_landlord_id')`.
- Money columns are `numeric` cents, never floats.
