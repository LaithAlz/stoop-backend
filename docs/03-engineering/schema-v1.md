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
>
> **v1.5 amendments (2026-07-05)** — migration 0008 implements this (#110),
> closing the schema-vocabulary gap flagged during #110's implementation:
> `conversation-model.md`'s "tenant confirms fixed → agent *proposes*
> resolution, landlord-visible, auto-applies after 48h unless contradicted"
> phase has no representable state without a durable, timer-shaped marker —
> `cases.status` has no pending/proposed value, and `audit_log.action` has
> no matching vocabulary entry (only the terminal `case_resolved`). Per the
> repo's own precedent ("the undo window is data, not a sleep" — the
> `drafts.scheduled_send_at` design above), the resolved answer is a new
> column, not a new implicit timer someone has to remember exists.
> 1. New nullable column `cases.pending_resolved_at timestamptz` (no
>    default, no backfill — every existing row has none pending). `NULL`
>    means "no proposal pending" (the overwhelmingly common case).
> 2. **Design choice — this column stores the APPLY-AT time, not the
>    proposal time**: when `identify_case` (`app/agent/case_lifecycle.py`'s
>    `propose_resolution`) sees a tenant confirm an issue is fixed, it sets
>    `pending_resolved_at = now() + 48h` directly, rather than storing the
>    proposal timestamp and having every reader re-derive the deadline.
>    Chosen over the proposal-time alternative because (a) the column name
>    is then self-describing (it literally names the moment the case
>    resolves, not an event that happened earlier), (b) the sweep query is
>    a trivial `pending_resolved_at <= now()` with no arithmetic or
>    duplicated `interval '48 hours'` literal to keep in sync between
>    `propose_resolution` and the sweep, and (c) a future change to the
>    proposal window (e.g. 48h → 24h) is a one-line constant change with no
>    migration, because the window is baked in at proposal time, not read
>    out at sweep time.
> 3. **Precedence over the 14-day auto-stale sweep**: a case with
>    `pending_resolved_at IS NOT NULL` is NEVER auto-staled, regardless of
>    how old its `last_activity_at` is — the pending, tenant-confirmed
>    resolution is a more specific and more recent signal than mere
>    inactivity, and auto-staling out from under it would silently discard
>    that signal. It resolves via exactly one of: the 48h deadline arrives
>    (`resolved_reason = 'tenant_confirmed'`), or a new message on the case
>    contradicts it first (`pending_resolved_at` cleared back to `NULL`,
>    case stays open/active — no `audit_log` entry for a contradiction,
>    since none was ever written for the proposal either; both are
>    reasoning_log-visible on the approval card, not audit facts).
> 4. `audit_log` vocabulary is UNCHANGED — the proposal and any
>    contradiction are visible via the column plus `reasoning_log` only;
>    an `audit_log` entry is written ONLY when the resolution actually
>    APPLIES (`action = 'case_resolved'`, same as every other resolution
>    path), matching the existing rule that `audit_log` records facts that
>    happened, not intentions that might not.
> 5. `pending_resolved_at` is cleared (`NULL`) on every path that resolves,
>    reopens, or re-proposes a case — a resolved/reopened case must never
>    carry a stale pending-resolution deadline forward.
>
> **v1.6 amendments (2026-07-05)** — no migration required; a schema
> contradiction found and resolved during #32's implementation
> (`classify_severity`), same class as the `messages.twilio_status`
> precedent above (v1.1 amendments):
> 1. `messages.classification` / `tokens_in` / `tokens_out` / `model` /
>    `llm_cost_cents` are DEPRECATED — never written. `messages` is
>    append-only (rule #2) and the row is INSERTED by the webhook handler
>    BEFORE classification ever runs, so populating these columns after
>    the fact would require an UPDATE on an append-only table — the same
>    contradiction the `twilio_status` deprecation above already resolved
>    for delivery status. Columns stay listed above (migration 0002
>    already shipped them) until their DROP in a future migration (not yet
>    scheduled/numbered).
> 2. The canonical classification record is instead an `audit_log` row:
>    `actor='agent'`, `action='classified'` (existing vocabulary),
>    `payload = {message_id, case_id, severity, rules_fired, modifier,
>    refusal_flags, model, tokens_in, tokens_out, cost_cents,
>    prompt_version}` — see `app/agent/nodes/classify_severity.py`'s
>    module docstring for the full rationale and
>    `docs/03-engineering/api-contracts.md`'s `GET /v1/cases/{id}` timeline
>    example, which this payload shape extends.

> **v1.7 amendment (2026-07-06)** — no migration required; found while
> amending the `/v1/queue` contract for the dashboard (PR #182 review):
> 1. The `audit_log` `'classified'` payload gains a **`summary`** key —
>    the ONE warm plain-English severity sentence the agent already
>    composes into `reasoning_log` (e.g. "No heat on a cold night with a
>    baby in the unit can't wait, so I treated it as urgent."). Payload
>    shape is now `{message_id, case_id, severity, summary, rules_fired,
>    modifier, refusal_flags, model, tokens_in, tokens_out, cost_cents,
>    prompt_version}`.
> 2. Why: `reasoning_log` lives only in transient graph state and opaque
>    checkpoint blobs — nothing durable/queryable serves the approval
>    card's margin note (`why` in `GET /v1/queue`). This REVISES
>    `classify_severity.py`'s "never duplicated into the audit trail"
>    ruling for exactly this one sentence: the audit row is the canonical
>    classification record (v1.6), so the landlord-facing summary belongs
>    on it. Code change (write the key) is tracked to land before or with
>    #56; rows written before it lack the key — readers treat a missing
>    `summary` as `null` (`/v1/queue` then returns `why: null`; any
>    friendlier fallback copy is a client concern, never synthesized from
>    `rules_fired` at the data layer).

> **v1.8 amendments (2026-07-11)** — migration 0009 implements these (#109,
> degraded mode / classification-failure handling,
> `docs/02-product/emergency-prefilter.md`'s degraded-mode table). Two new
> `notifications.type` values — no new column/table, per the "adding a
> value is an ALTER ... DROP/ADD CONSTRAINT" note above; same evolution
> path v1.1 already used for `messages.party`:
> 1. **`tenant_ack`** (`channel='sms'`) — the durable, NOT-YET-SENT holding
>    -ack SMS to the tenant when classification fails (the templated
>    "Got your message — it's been passed to ⟨landlord first name⟩..."
>    text, `emergency-prefilter.md`'s "Holding ack" section — amended
>    2026-07-12, copy-guardian ruling: the "...and you'll hear back soon"
>    clause was removed, see that doc's own note). Exactly the same
>    shape as the existing, already-anticipated-but-unused `emergency_sms`
>    row (a durable send-intent for #108's future sender to drain) — kept
>    as its OWN type rather than reusing `emergency_sms` because the two
>    are semantically different sends (a category-templated safety
>    instruction vs. a generic holding ack) that a future sender must be
>    able to tell apart, and reusing `needs_eyes` was considered and
>    rejected: `uq_notifications_message_dedupe` keys on `(message_id,
>    type)` only, so a tenant-facing row sharing `needs_eyes`'s type would
>    silently consume the SAME dedupe slot the landlord-facing
>    notification for that message needs — the two must never collide.
>    Idempotent via a new partial unique index, `uq_notifications_
>    tenant_ack_dedupe ON notifications ((payload ->> 'message_id')) WHERE
>    type = 'tenant_ack'` — same NULL-safe pattern as
>    `uq_notifications_message_dedupe` (v1.3 amendments above). `status`
>    stayed `'pending'` until #108's sender existed to drain it (this
>    issue, #109, never sent anything itself) — **closed 2026-07-12**:
>    `app/agent/emergency_chain.py::run_sms_drain_sweep` (spec finding S1)
>    now drains every `pending`/`failed` `tenant_ack` row on each
>    scheduler tick, resending on failure until genuinely delivered. See
>    `app/agent/degraded_mode_sweep.py`'s own module docstring
>    ("DEPLOYMENT-GATING FACT") for the full closure note. **Safety review,
>    2026-07-12 (finding N2):** `'failed'` is TRANSIENT-only (stays in the
>    drain sweep's own retry set); a genuinely TERMINAL outcome for
>    `tenant_ack`/`emergency_sms` (today: no stored tenant phone to send
>    to at all — see `emergency_chain.py`'s own "Known limitation") lands
>    on `'exhausted'` instead, the SAME terminal value #2 below already
>    uses — no new CHECK value, just reusing the existing one for a second
>    type.
> 2. **`degraded_retry`** (`channel='push'` — placeholder; this type is
>    never delivered to anyone, see below) — an internal-only marker for
>    the "no keywords at all" degraded-mode leg: classification failed,
>    nothing (HARD or SOFT) fired the prefilter, so the landlord is NOT
>    paged immediately. Instead this row schedules re-classification
>    attempts at `failed_at + {1, 5, 15} minutes` via the EXISTING
>    `next_attempt_at` sweeper-key column (no new column needed) — a
>    future scheduled sweep (this issue adds the pure function only, see
>    `app/agent/degraded_mode_sweep.py`; cron/scheduler wiring is a later
>    issue's seam, same pattern as `case_lifecycle.sweep_cases()`)
>    re-attempts classification each time it comes due. `status` is
>    `'pending'` while in flight and `'exhausted'` once the chain
>    concludes ONE OF TWO ways, recorded in `payload.outcome`:
>    `"resolved"` (a later attempt succeeded — nothing further happens,
>    this row is simply inert from then on) or `"escalated"` (all three
>    attempts failed — a genuine, separate `needs_eyes` row is inserted at
>    that point, via the same `uq_notifications_message_dedupe` pattern
>    every other `needs_eyes` insert in this codebase already uses).
>    `degraded_retry` rows are NEVER read/interpreted by anything except
>    this sweep — deliberately kept out of `needs_eyes`'s own lifecycle
>    (which always means "ready to tell a person, now or as soon as
>    delivery infra exists") so a future notification-delivery consumer
>    can safely treat every `needs_eyes` row as delivery-ready without
>    needing to know about an in-flight retry hold. Idempotent via its own
>    partial unique index, `uq_notifications_degraded_retry_dedupe ON
>    notifications ((payload ->> 'message_id')) WHERE type =
>    'degraded_retry'`.
> 3. Both new types are covered by the SAME "never delete" rule the v1.3
>    amendments already state for `emergency_call`/`needs_eyes` — `tenant_ack`
>    anchors its own dedupe index, and `degraded_retry` is the durable
>    record of "did this message's classification ever recover on its
>    own" (an audit-adjacent fact, not just a scratch row).

> **v1.9 amendment (2026-07-12)** — migration 0010 implements this (#108
> safety review, finding 8, LOW/doc-first). One new expression index, no
> new column or table:
> 1. **`uq_notifications_ack_token`** — `CREATE UNIQUE INDEX
>    uq_notifications_ack_token ON notifications ((payload ->> 'ack_token'))
>    WHERE payload ->> 'ack_token' IS NOT NULL`. The emergency escalation
>    chain (`app/agent/emergency_chain.py`) generates a random,
>    unguessable `ack_token` (`secrets.token_urlsafe(24)`) into an
>    `emergency_call` row's `payload` at T+0 and looks it up on every
>    `GET`/`POST /ack/{token}` request — without an index this is a
>    sequential scan over the whole table on every tap of an SMS link.
>    UNIQUE doubles as a data-integrity guarantee (two rows should never
>    share the same token) — safe under the same NULL-handling Postgres
>    already uses for every other partial unique index in this file: rows
>    with no `ack_token` key (every type except `emergency_call`, and
>    even `emergency_call` rows before `handle_emergency_trigger` enriches
>    them) extract SQL `NULL` via `->>`, and Postgres unique indexes never
>    treat two `NULL`s as equal, so those rows never collide with each
>    other or with a real token. The `WHERE ... IS NOT NULL` partial
>    predicate additionally keeps the index itself small (only rows that
>    actually carry a token are indexed at all).
> 2. **Round-trip / downgrade**: `downgrade()` drops the index — always
>    safe, unlike migration 0009's CHECK-widening (there is no CHECK
>    constraint here to narrow, so there is no "existing rows violate the
>    restored constraint" failure mode to fail closed against). Dropping
>    this index does not lose data (the `ack_token` values themselves live
>    in `payload`, untouched) — it only means token lookups fall back to a
>    sequential scan until the migration is re-applied, a performance
>    regression, never a correctness one.
>
> **v1.10 amendment (2026-07-13)** — no migration required (#197); the
> column already exists (this doc, since v1.0) but no code path ever wrote
> it — flagged during #56's implementation (queue.py's own module
> docstring) and escalated after the #50 e2e rehearsal found it hard
> -blocking #60's trust ladder (`trust_metrics` never accumulated because
> nothing populated `cases.severity`):
> 1. `app/agent/nodes/classify_severity.py` now writes `cases.severity`,
>    in the SAME admin-session transaction as its `audit_log` `'classified'`
>    INSERT (v1.6 above) — the value is the exact post-clamp
>    `severity_result.severity.db_value` that INSERT already records, never
>    a second, independently-derived value; the two can never disagree.
>    Skipped when the message has no case yet (the unknown-sender fallback
>    thread — nothing to update).
> 2. **Never downgrade a case away from `'emergency'`** — the CASE-LEVEL
>    mirror of the Tier-0 clamp's own "escalate past a miss, never
>    de-escalate a fire" invariant, extended across separate classification
>    calls on the same case: a case already at `'emergency'` stays
>    `'emergency'` even if a later message on that case classifies as
>    `'urgent'`/`'routine'` on its own textual merits. Enforced by the
>    `UPDATE`'s own `WHERE severity IS DISTINCT FROM 'emergency'` clause
>    (atomic, race-free — no read-then-branch in application code). This
>    guard is narrower than a full monotonic clamp: an `'urgent'` case CAN
>    still be overwritten by a later `'routine'` classification — only
>    `'emergency'` is sticky, matching the Tier-0 clamp's own scope.
> 3. **The `audit_log` `'classified'` row remains the canonical,
>    historical classification record** (v1.6 above, unchanged) —
>    `cases.severity` is a mutable "current state" pointer derived from it,
>    not a replacement for it. `GET /v1/queue` and `GET /v1/cases`(`/{id}`)
>    read-side sourcing is UNCHANGED by this amendment: `queue.py` still
>    reads the latest classified audit row (it also needs `rules_fired`/
>    `summary`, which don't live on `cases` at all — see that router's own
>    module docstring); `cases.py` already read `cases.severity` directly
>    (returning `null` for every real case until this amendment), so its
>    responses start reflecting real data with no code change. Switching
>    `queue.py` to read `cases.severity` instead is a plausible follow-up,
>    not done here.
> 4. **No backfill.** Cases created before this amendment keep
>    `severity IS NULL` forever unless/until a later message on that same
>    case triggers a fresh classification — exactly the same "no backfill,
>    NULL stays legal" precedent `pending_resolved_at` set in the v1.5
>    amendments above.
> 5. `cases.title` remains unwritten — explicitly deferred, out of this
>    amendment's scope (tracked separately).

> **v1.11 amendment (2026-07-13)** — migration 0011 implements this (#53,
> property provisioning; renumbered from v1.10 — PR #202's `cases.severity`
> amendment took that label first). No new column or table on `properties` —
> `twilio_number`/`twilio_sid` already existed for exactly this — but
> deprovisioning's grace period needs one new durable, sweep-visible
> artifact, and `properties` has no `deleted_at`/status column to hang it
> off (`DELETE /v1/properties/{id}` is a genuine hard delete, unchanged).
> Same evolution path as v1.3/v1.8 above (adding a `notifications.type`
> CHECK value, not a new column/table):
> 1. **`number_release`** (`channel='push'`, same "internal marker, never
>    actually delivered to a person" convention `degraded_retry` already
>    established) — the durable record of "release this Twilio number back
>    to the pool," written by `DELETE /v1/properties/{id}` at the moment a
>    property (with a live `twilio_number`) is deleted, since the actual
>    external release is deliberately NOT synchronous with the request
>    (`apps/api/CLAUDE.md`'s "windows are data, not sleeps" — mirrors the
>    approve-flow's `scheduled_send_at` undo window). `payload` carries
>    `twilio_sid`/`property_id`/`landlord_id` only (uuids/SIDs — rule #5);
>    the property row itself is already gone by the time this is written,
>    so this row is the ONLY remaining record of which Twilio number needs
>    releasing. `next_attempt_at` is set to `now() + 24h` at creation (the
>    grace period itself — a same-day window during which the number still
>    physically exists in the Twilio account even though the `properties`
>    row does not) and the EXISTING `idx_notifications_sweep (status,
>    next_attempt_at)` index already serves it — no new index needed here,
>    unlike v1.3/v1.8's per-type dedupe indexes. `status` lifecycle:
>    `pending` → `sent` (released) or, after
>    `app/property_provisioning.py`'s bounded retry count is exhausted,
>    `exhausted` (same terminal-vs-transient convention the v1.8 amendments
>    already established for `tenant_ack`/`degraded_retry`).
> 2. **`uq_notifications_number_release_dedupe`** — `CREATE UNIQUE INDEX
>    ... ON notifications ((payload ->> 'twilio_sid')) WHERE type =
>    'number_release'`, same NULL-safe partial-unique pattern as every
>    other dedupe index in this file. Guards a genuine (if narrow) race: two
>    concurrent `DELETE` requests for the same property could both read the
>    row's `twilio_sid` before either commits the delete; this makes
>    scheduling the release idempotent regardless.
> 3. `DELETE /v1/properties/{id}` itself gains a required `confirm=true`
>    query parameter (contract-level only, no schema change) — see
>    `api-contracts.md`'s Properties section.

> **v1.12 amendment (2026-07-14)** — no migration required (#111, per-message
> cost metering, `architecture.md` §9's "cost per door must be a query, not
> a guess"). No new `action`/`type` CHECK value — `'sent'` and
> `'emergency_call_attempt'` are already-legal `audit_log.action` values
> (v1.0/v1.3), unchanged. Two existing payload shapes gain new keys, same
> "doc-first, no migration" evolution path v1.7's `summary` key amendment
> already used:
> 1. The `'sent'` payload (`app/agent/draft_sender.py::_INSERT_SENT_AUDIT_SQL`
>    — the only writer of this action) gains **`segments`** (integer — GSM-7/
>    UCS-2 segment count of the sent body) and **`sms_cost_cents`** (numeric
>    — estimated Twilio per-segment cost). Shape is now `{draft_id,
>    message_id, edited, segments, sms_cost_cents}`. Computed from the
>    already-in-scope `body`/`final_body` at the SAME `INSERT` that already
>    writes `message_id` — never an `UPDATE`.
> 2. The `'emergency_call_attempt'` payload (`app/agent/emergency_chain.py`)
>    gains **`property_id`** (the candidate's own property — lets a cost
>    rollup group by door without a second join through `notifications`)
>    at the top level, and each entry of its existing `actions` array gains
>    **`segments`**/**`sms_cost_cents`** for SMS-type actions only
>    (`landlord_sms`, `backup_sms`, `tenant_safety_sms`, `tenant_status_sms`)
>    — both `null` for voice-call actions (`landlord_call`/`backup_call`,
>    priced per-minute by Twilio, out of this issue's scope) and for
>    `skipped`/`failed` outcomes (no SMS actually went out).
> 3. **Flagged, not resolved:** `messages.sms_cost_cents` (this table, still
>    listed below, never marked DEPRECATED at v1.6 unlike its four LLM-cost
>    siblings — `classification`/`tokens_in`/`tokens_out`/`model`/
>    `llm_cost_cents`) is still not written by this amendment, even though
>    the ONE outbound `messages` row this codebase ever inserts
>    (`app/agent/draft_sender.py::_INSERT_OUTBOUND_MESSAGE_SQL`) is written
>    *after* the real Twilio send completes — so, unlike the v1.6 LLM
>    columns (inserted *before* classification runs), populating
>    `sms_cost_cents` at that same INSERT would NOT be an append-only
>    violation. RESOLVED by spec-guardian adjudication in this same
>    amendment: writing it would create a second source of truth for a fact
>    whose only reader (`app/cost_reporting.py`) deliberately reads
>    `audit_log` alone — exactly the redundancy/drift risk v1.6 eliminated
>    for LLM cost. The column is therefore marked **DEPRECATED v1.12**
>    below, alongside its four v1.6 siblings; point 1's `audit_log`
>    `'sent'` payload is the canonical SMS-cost record.
> 4. New pure-function helper: `app/integrations/sms_segments.py` — GSM-7 vs
>    UCS-2 segment counting (extended-table GSM-7 characters count double;
>    any character outside the GSM-7 repertoire falls the WHOLE message back
>    to UCS-2) plus a conservative, FOUNDER-PROVISIONAL per-segment price
>    constant (Twilio's long-published CA outbound SMS rate, $0.0075
>    USD/segment) — same "hardcoded, erring-high placeholder, reconcile
>    against real billing later" pattern `app/integrations/anthropic.py`'s
>    `estimate_cost_cents` already established for LLM cost.
> 5. Cost-per-case / cost-per-door(property) / cost-per-month is answered by
>    three plain parameterized queries over one shared `audit_log`-only CTE
>    — `app/cost_reporting.py` — never a database VIEW or a migration (no
>    new column or table needed here; a view would only add a migration to
>    keep hand-in-sync with the same small query, not reduce complexity).
>    Reads `'classified'`/`'drafted'` payloads for LLM cost and
>    `'sent'`/`'emergency_call_attempt'` payloads for SMS cost; rows written
>    before this amendment (missing the new keys) contribute `0`, never an
>    error — every cast is guarded by a `payload ? 'key'` existence check
>    first. Caveat (mirrored from `app/cost_reporting.py`'s docstring):
>    per-CASE rollups structurally exclude emergency-chain SMS cost — the
>    `emergency_call_attempt` row carries no `case_id` by design (it fires
>    in the webhook before `identify_case` runs) — that cost appears in the
>    per-property and per-month rollups instead.
> 6. **Reconciliation note:** verified at review time — the sibling
>    `feat/audit-completeness` branch (merged as PR #207) makes no schema
>    changes, so v1.12 is uncontested.

> **v1.13 amendment (2026-07-18)** — migration 0012 implements this (#210
> M3, push-notifications backend surface). Push is for approvals/status
> only — **push never carries the emergency path** (rule #1: voice + SMS
> remain the only emergency channels; nothing here touches, delays, or
> conditions the escalation chain in `notifications`/`emergency_chain.py`).
> No feature-flag reads anywhere near this (rule #7). **Not to be confused
> with `notifications.channel = 'push'`** (v1.8/v1.11's `degraded_retry`/
> `number_release` types) — that is a purely INTERNAL scheduling marker,
> never actually delivered to a person (those rows' own amendments say so
> explicitly). `push_outbox` below is the opposite: every row it holds is
> a REAL Expo push notification, actually delivered to a landlord's
> device.
> 1. **Reused `push_tokens` — did NOT invent a `device_tokens` table.**
>    This table has existed since v1.0/migration 0002 (RLS'd since
>    migration 0005) but had no real writer until now. Per CLAUDE.md rule 6
>    ("schema names come from schema-v1.md ... never invent a variant"),
>    the M3 spec's working name (`device_tokens`/`expo_push_token`) is
>    superseded by this existing table/column — `push_tokens.token` is
>    exactly an Expo push token string (`ExponentPushToken[...]`) once a
>    real writer exists; no rename needed. `platform`'s existing CHECK
>    (`'ios','android','web'`) is left UNCHANGED (unnarrowed) — the mobile
>    registration endpoint's own request model only ever sends
>    `'ios'`/`'android'` (Expo has no `'web'` push-token concept), but
>    narrowing the stored CHECK for a value nothing has ever written was
>    judged not worth the churn against `tests/test_rls_isolation_matrix.py`
>    's existing `'web'`-literal seed data, with zero functional benefit.
> 2. **New nullable column `push_tokens.revoked_at timestamptz`** — set
>    ONLY by the push sweep (`app/push_outbox.py::run_push_outbox_sweep`)
>    when Expo's per-receipt response reports `DeviceNotRegistered` (the
>    token is permanently dead — reinstalls/uninstalls issue a fresh one).
>    `NULL` = active (the overwhelmingly common case; every existing row
>    stays `NULL`, no backfill). The enqueue seam (`push_outbox` INSERT,
>    below) only ever selects `revoked_at IS NULL` rows. This is a SOFT
>    marker, deliberately distinct from `DELETE /v1/devices/{id}`'s HARD
>    delete (api-contracts.md's Devices section) — an explicit landlord
>    sign-out unregister means "this row should not exist," while a
>    sweep-observed dead token is a passive delivery fact worth keeping
>    for observability, not an explicit user action.
> 3. **Token ownership model**: `token` stays `UNIQUE NOT NULL` (unchanged)
>    — `POST /v1/devices` upserts `ON CONFLICT (token) DO UPDATE SET
>    landlord_id = EXCLUDED.landlord_id, platform = EXCLUDED.platform,
>    last_seen_at = now(), revoked_at = NULL`. A token belongs to whoever
>    registered it LAST — the shared-device/sign-out-sign-in flow (landlord
>    B signs into a phone landlord A was previously using) safely moves the
>    row to landlord B, and the `revoked_at = NULL` reset on every upsert
>    means a token Expo once reported dead is trusted again the instant
>    a real registration call proves it live once more.
> 4. **New table `push_outbox`** — the durable delivery queue, one row per
>    `(device, event)` fan-out. Follows the `notifications` table's
>    sweep-column pattern EXACTLY (`status`/`attempt`/`next_attempt_at`/
>    `payload`/`updated_at`) — same CAS-claim discipline
>    (`app/agent/emergency_chain.py`'s `_CLAIM_STEP_SQL` /
>    `app/property_provisioning.py`'s `_CLAIM_NUMBER_RELEASE_SQL`) governs
>    its sweep:
>    ```sql
>    CREATE TABLE push_outbox (
>      id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
>      landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
>      device_token_id uuid NOT NULL REFERENCES push_tokens(id) ON DELETE CASCADE,
>      kind            text NOT NULL CHECK (kind IN ('draft_awaiting_approval')),
>      payload         jsonb NOT NULL DEFAULT '{}',
>      status          text NOT NULL DEFAULT 'pending'
>                      CHECK (status IN ('pending','sent','failed','exhausted')),
>      attempt         integer NOT NULL DEFAULT 0,
>      next_attempt_at timestamptz,
>      created_at      timestamptz NOT NULL DEFAULT now(),
>      updated_at      timestamptz NOT NULL DEFAULT now()
>    );
>    CREATE INDEX idx_push_outbox_sweep    ON push_outbox (status, next_attempt_at);
>    CREATE INDEX idx_push_outbox_landlord ON push_outbox (landlord_id);
>    CREATE INDEX idx_push_outbox_device   ON push_outbox (device_token_id);
>    ```
>    `payload` carries uuids/counts ONLY — `case_id`/`draft_id` for the v1
>    `'draft_awaiting_approval'` kind — NEVER tenant names/phones/message
>    bodies (rule #5-adjacent: push payloads transit Apple/Google servers).
>    The push notification body the app renders is a FIXED, generic string
>    ("A reply is waiting for your approval") never derived from `payload`.
>    Both FKs `ON DELETE CASCADE` (matches `push_tokens`' own
>    landlord-cascade convention above) — this table is best-effort
>    delivery bookkeeping, not an audit trail; losing rows when their
>    landlord/device is gone is correct, not a gap.
> 5. **The enqueue seam**: `app/agent/nodes/await_approval.py::
>    mark_awaiting_approval` — the SAME admin-session transaction that
>    flips `cases.status = 'awaiting_approval'` also runs one
>    `INSERT ... SELECT` fanning out to every one of that case's
>    landlord's active (`revoked_at IS NULL`) `push_tokens` rows, joined
>    through the case's own just-inserted pending draft. Zero devices or
>    zero pending draft (the rare `draft_response` race-exhausted path —
>    see that node's own docstring) both naturally yield zero rows via the
>    `JOIN`s themselves — no branching needed, and a hard DB error rolls
>    back both statements together (same transaction, by design — see
>    that module's docstring for why this is acceptable). A `NOT EXISTS`
>    guard on the same statement additionally makes a crash-then-redelivered
>    run of this node a no-op for any `(device, draft)` pair already
>    enqueued (`app/agent/graph_entry.py`'s own documented "Crash-window
>    coherence with #43's mark_awaiting_approval" — this node CAN genuinely
>    re-run for the same message) — no new unique index needed since
>    `app/agent/graph.py`'s per-case advisory lock already rules out a true
>    concurrent race here, only a sequential redelivery one.
> 6. **The sweep** (`app/push_outbox.py::run_push_outbox_sweep`, wired
>    into `app/scheduler.py`'s tick LAST, after `number_release`) claims a
>    bounded batch via CAS, calls Expo's push API once per claimed row
>    (raw `httpx`, no Expo SDK dependency — mirrors
>    `app/integrations/twilio_send.py`'s client pattern), and applies:
>    `DeviceNotRegistered` → `push_tokens.revoked_at` set + this row
>    → `'failed'` (TERMINAL for this cause, unlike the `tenant_ack`/
>    `emergency_sms` convention where `'failed'` is transient — see
>    `app/push_outbox.py`'s own docstring); any other error (HTTP failure,
>    Expo-reported transient ticket error) → the house bounded-retry
>    backoff (`app/property_provisioning.py::sweep_pending_number_
>    releases`'s shape: fixed interval, bounded attempt count) → `'sent'`
>    on eventual success or `'exhausted'` once the bound is reached.
>    **Deliberate divergence from every other sweep in this codebase:
>    exhaustion here pages NO Sentry alert** — push is best-effort by
>    design (a landlord who never registered a device, or whose push
>    fails, loses nothing; the queue/SMS surfaces are the source of
>    truth), so a log line is the only signal, never a page.
> 7. **RLS + grants** — migration 0012 is the first migration since 0005
>    to add a genuinely new table, so it independently reproduces 0005's
>    exact pattern for `push_outbox` (no prior migration 0006-0011 added a
>    table to mirror — all six were column/index/CHECK-only amendments):
>    `ENABLE ROW LEVEL SECURITY` (no `FORCE`, same rationale as every other
>    table), one `FOR ALL TO app_role` policy keyed on direct `landlord_id`
>    match, and `GRANT SELECT, INSERT, UPDATE, DELETE ... TO app_role`.
>    The anon/authenticated Data API closure needs no new statement here —
>    migration 0005's `ALTER DEFAULT PRIVILEGES` already covers every
>    table the SAME migrating role creates in `public` in the future,
>    which includes this one. **Flagged for the live-dry-run rule**: this
>    migration is RLS/role-grant-adjacent, so per that rule it must be
>    dry-run against a real (non-local) Supabase-shaped database before
>    merge, same as migration 0005 itself was.

> **v1.14 amendment (2026-07-21)** — no migration required (#208, "failed
> -after-real-API-call Anthropic attempts are invisible to cost rollups").
> No new `action` CHECK value and no new column: both fixes below are
> payload-only, the same "doc-first, no migration" path v1.6/v1.7/v1.10/
> v1.12 already used. Investigated end-to-end before picking this design
> (see `app/agent/nodes/classify_severity.py`/`classify_intent.py`'s own
> module docstrings for the full per-node rationale) — the honest options
> were (a) a payload-only amendment onto a RELIABLY-existing failure-path
> row, wherever one exists, or (b) a new `audit_log.action` CHECK value +
> migration. (a) was possible for both affected nodes without inventing
> anything:
> 1. **`classify_severity` → the EXISTING `'degraded_mode'` row.** This
>    node itself still writes NO audit row on any failure (unchanged) —
>    but `app.agent.graph` unconditionally routes
>    `classification_failed=True` to `app.agent.nodes.degraded_mode`,
>    which reliably writes exactly one `'degraded_mode'` row for a genuine
>    new activation (existing idempotency, unchanged). When the failed
>    attempt(s) genuinely reached the API and consumed billed tokens (a
>    response was received but this node's own `SeverityResult` validation
>    rejected it, or the SDK's forced-`tool_choice` response carried no
>    usable `tool_use` block), that row's payload gains **`model`**
>    (nullable text), **`tokens_in`**/**`tokens_out`** (integer), and
>    **`cost_cents`** (numeric) — summed across both attempts' reached-the
>    -API usage. Absent entirely when neither attempt ever reached the API
>    (a pure connection/timeout failure) — never a fabricated zero-cost
>    key. `app/cost_reporting.py` gained one new CTE branch,
>    `WHERE a.action = 'degraded_mode' AND a.payload ? 'cost_cents'`,
>    guarded by the same key-existence check as every other branch.
> 2. **`classify_intent` → a NEW row on the EXISTING `'classified'`
>    action.** Unlike `classify_severity`, a double-failed intent
>    classification has no downstream node to piggyback on (nothing routes
>    on `state["intent"]` yet). This node now writes one additional
>    `'classified'` row of its own on total failure, but ONLY when at least
>    one attempt genuinely reached the API: `payload = {kind:
>    'intent_classification_failed', message_id, case_id, model,
>    tokens_in, tokens_out, cost_cents}` — deliberately no `intent`/
>    `summary`/`is_new_issue` keys, so this payload never claims a
>    classification actually happened. Matches the EXISTING
>    `action IN ('classified', 'drafted') AND payload ? 'cost_cents'` CTE
>    branch verbatim — no query change needed for this half of the fix.
> 3. **`draft_response` — bug fix, no schema change at all.** This node
>    already writes an unconditional `'drafted'` row every run (the safe
>    generic fallback body when both attempts fail) and already summed
>    reached-the-API usage across attempts into that row's existing
>    `cost_cents`/`tokens_in`/`tokens_out` — but only for attempts whose
>    OWN `DraftResult` validation also succeeded, silently dropping a
>    reached-the-API attempt that failed ONLY that validation step. Fixed
>    in code (accumulate right after the Anthropic call succeeds, before
>    validating it), not in this doc — no new payload key, no migration.
> 4. **Retry-then-success accounting, stated explicitly (differs by
>    node, on purpose):** `draft_response`'s single `'drafted'` row sums
>    EVERY attempt's reached-the-API usage into one payload, success or
>    failure alike (unchanged design, now bug-fixed to actually do this).
>    `classify_severity`/`classify_intent`'s SUCCESS row (`'classified'`)
>    is BYTE-IDENTICAL to before this amendment — it still carries only
>    the WINNING attempt's usage. A first attempt that reaches the API and
>    fails, followed by a second that succeeds, therefore still loses that
>    first attempt's cost for these two nodes specifically — a known,
>    accepted, explicitly-scoped-out gap (folding a rejected attempt's cost
>    into a row that represents "this is what got classified" is a bigger
>    design question than this amendment's "make TOTAL failure visible"
>    scope), not silently assumed fixed.
> 5. **Numbering note, confirmed:** push-backend's `v1.13` (#210, directly
>    above) is already on `main` (merged as PR #220) by the time this
>    amendment was rebased — so this #208 amendment is `v1.14`, not
>    `v1.13`. Needing no migration does NOT make a doc-heading amendment
>    number uncontended on its own (v1.10/v1.12 needed no migration either
>    and still consumed a real slot) — same collision class as v1.11's own
>    precedent above ("renumbered from v1.10 — PR #202 took that label
>    first"), just resolved at rebase time instead of at merge time.

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
                                                        -- v1.10: written by classify_severity,
                                                        --  post-clamp, never downgraded away
                                                        --  from 'emergency'; audit_log
                                                        --  'classified' stays the historical
                                                        --  record. NULL = never classified
                                                        --  (pre-v1.10 cases; no backfill).
  intent              text,                          -- maintenance|admin|question|other
  title               text,                          -- short agent-written summary
  langgraph_thread_id text UNIQUE NOT NULL,
  related_case_id     uuid REFERENCES cases(id),     -- >30d reopen → new case, linked
  emergency_fired_at  timestamptz,                   -- dedupe: protocol fires once per case
  last_activity_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at         timestamptz,
  pending_resolved_at timestamptz,                    -- v1.5 (migration 0008, #110): tenant said
                                                        --  "all fixed" at T -> resolution auto-
                                                        --  applies at T+48h unless contradicted;
                                                        --  NULL = no proposal pending
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
  classification  jsonb,                             -- DEPRECATED v1.6: never written; this row
                                                     --  is inserted BEFORE classification runs
                                                     --  (append-only). Canonical record is
                                                     --  audit_log 'classified' (#32). DROP
                                                     --  scheduled in a future migration. Shape
                                                     --  was {severity, rules_fired, modifier,
                                                     --  refusal_flags, reasoning}
  tokens_in       integer,                           -- DEPRECATED v1.6: never written; see
                                                     --  `classification` above
  tokens_out      integer,                           -- DEPRECATED v1.6: never written; see
                                                     --  `classification` above
  model           text,                              -- DEPRECATED v1.6: never written; see
                                                     --  `classification` above
  llm_cost_cents  numeric(10,4),                      -- DEPRECATED v1.6: never written; see
                                                     --  `classification` above
  sms_cost_cents  numeric(10,4),                     -- DEPRECATED v1.12: never written; the
                                                     --  audit_log 'sent' payload is canonical
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
                    'needs_eyes','draft_ready','recap','tenant_ack','degraded_retry',
                    'number_release')),
                                                     -- 'tenant_ack'/'degraded_retry' added v1.8 (#109)
                                                     -- 'number_release' added v1.11 (#53)
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

-- v1.8 (migration 0009, #109): same NULL-safe, per-type dedupe pattern,
-- one partial unique index per new type -- see the v1.8 amendments note
-- above for why these are separate from uq_notifications_message_dedupe.
CREATE UNIQUE INDEX uq_notifications_tenant_ack_dedupe
  ON notifications ((payload ->> 'message_id'))
  WHERE type = 'tenant_ack';
CREATE UNIQUE INDEX uq_notifications_degraded_retry_dedupe
  ON notifications ((payload ->> 'message_id'))
  WHERE type = 'degraded_retry';

-- v1.11 (migration 0011, #53): deprovisioning's grace-period release --
-- see the v1.11 amendments note above. Reuses idx_notifications_sweep
-- (status, next_attempt_at) above for the sweep itself; this index is only
-- for idempotency, keyed on twilio_sid rather than message_id (this type
-- has no message_id at all).
CREATE UNIQUE INDEX uq_notifications_number_release_dedupe
  ON notifications ((payload ->> 'twilio_sid'))
  WHERE type = 'number_release';

-- ───────────────────────── push_tokens ───────────────────────
CREATE TABLE push_tokens (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id  uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
  token        text NOT NULL UNIQUE,          -- an Expo push token once a real writer
                                               --  exists (v1.13); ON CONFLICT (token) is
                                               --  the "last registration wins" upsert key
  platform     text NOT NULL CHECK (platform IN ('ios','android','web')),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  revoked_at   timestamptz,                   -- v1.13 (migration 0012): set by the push
                                               --  sweep on Expo's DeviceNotRegistered;
                                               --  NULL = active. DELETE /v1/devices/{id}
                                               --  hard-deletes the row instead (see v1.13
                                               --  amendments above).
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── push_outbox ───────────────────────
-- Durable delivery queue for landlord-facing push notifications (#210 M3)
-- -- approvals/status only, NEVER the emergency path (rule #1). See the
-- v1.13 amendments block above for the enqueue seam and sweep design.
CREATE TABLE push_outbox (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  landlord_id     uuid NOT NULL REFERENCES landlords(id) ON DELETE CASCADE,
  device_token_id uuid NOT NULL REFERENCES push_tokens(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('draft_awaiting_approval')),
  payload         jsonb NOT NULL DEFAULT '{}', -- uuids/counts ONLY -- never tenant
                                                --  names/phones/message bodies
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','sent','failed','exhausted')),
  attempt         integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_push_outbox_sweep    ON push_outbox (status, next_attempt_at);
CREATE INDEX idx_push_outbox_landlord ON push_outbox (landlord_id);
CREATE INDEX idx_push_outbox_device   ON push_outbox (device_token_id);

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
