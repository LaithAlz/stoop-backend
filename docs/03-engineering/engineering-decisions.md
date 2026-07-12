# Engineering Decisions — `apps/api`

> **Status:** Living reference, started 2026-07-05. Covers PRs #123–#177.
> This is the durable record of *why* — every load-bearing backend decision
> that isn't self-evident from reading the diff. `architecture.md` says what
> the system is; `schema-v1.md` says what the tables are; this doc says why
> a given line is the way it is when that reason was hard-won (a live
> Supabase finding, a safety-review reversal, a spec-guardian ruling). One
> entry per decision: **what** was decided, **why** (the forcing constraint
> or finding), **where** it lives. Reference, not narrative — read the
> section you need, not the whole thing.

---

## 1. Postgres platform — Supabase constraints and the Supavisor pooler

### The Supavisor transaction-pooler compatibility recipe (three knobs)

**What:** Every async engine (`app/db/session.py`, `migrations/env.py`) is
built with a shared `_ASYNCPG_POOLER_CONNECT_ARGS` dict of exactly three
`connect_args`:
```python
{
    "prepared_statement_cache_size": 0,          # SQLAlchemy's asyncpg-dialect cache
    "prepared_statement_name_func": _asyncpg_prepared_statement_name,  # uuid4-based name
    "statement_cache_size": 0,                   # asyncpg's OWN driver-level cache
}
```
**Why:** Supabase's pooler (Supavisor, port 6543, transaction mode)
multiplexes many logical sessions across a small set of physical backends,
handing a *different* backend to the same connection between transactions.
The first live run against Supabase (ca-central-1) hit
`asyncpg.exceptions.DuplicatePreparedStatementError` — two sessions
independently landing on `__asyncpg_stmt_1__` on a shared backend. The
two-knob fix SQLAlchemy's own asyncpg-dialect docstring documents
(`prepared_statement_cache_size=0` + `prepared_statement_name_func`) was
proven *insufficient* by live load testing: 18/100 requests still failed.
Root cause of the residual 18%: `pool_pre_ping=True`'s connection ping
(`AsyncAdapt_asyncpg_connection.ping()` → raw `fetchrow(";")`) bypasses the
SQLAlchemy dialect's `_prepare()` entirely and hits asyncpg's own
convenience-method cache with asyncpg's own sequential auto-naming — a
separate cache the first two knobs don't touch. Adding the third,
undocumented-in-SQLAlchemy knob (`statement_cache_size=0`, asyncpg's own
driver-level cache, no `prepared_` prefix) closed the gap: 0/100 failures,
confirmed by reverting and re-applying the knob to prove it load-bearing.
This is exactly the fix asyncpg's own error hint recommends ("you can set
`statement_cache_size` to 0 when creating the asyncpg connection object").
Harmless against a direct/local Postgres connection — only disables
opportunistic caching.
**Where:** `apps/api/app/db/session.py` (`_ASYNCPG_POOLER_CONNECT_ARGS`,
`_asyncpg_prepared_statement_name`), mirrored in
`apps/api/migrations/env.py`; PR #165. Test coverage:
`apps/api/tests/test_db_engine.py` reads the *actually-wired* `cparams` off
the live engine's connection factory closure, not just the source constant.

### `pool_pre_ping=True` — the discovery, not just the setting

**What:** `pool_pre_ping=True` on both engines, `pool_recycle=300` (shorter
than Supabase's idle-timeout window).
**Why:** Necessary against Supabase's idle-connection behavior — but its
ping path is *also* the thing that exposed the third pooler knob above
(see previous entry). The two facts are linked: enabling the safety
setting that stale sockets require is precisely what surfaced the second,
independent statement-cache bug.
**Where:** `apps/api/app/db/session.py` (engine factory, `pool_pre_ping`/
`pool_recycle` block).

### Supabase platform facts that shaped the role/RLS design (migrations 0004/0005)

**What — four live-verified facts, none of which any design doc predicted:**
1. **`postgres` is not a superuser on Supabase**, and neither is
   `service_role` — both instead carry `rolbypassrls = TRUE`. Confirmed by
   a live `SELECT rolname, rolsuper, rolbypassrls FROM pg_roles` query,
   quoted verbatim in the migration docstring.
2. **`pg_has_role(current_user, <role>, 'MEMBER')` is unsound as an
   idempotency guard on Postgres 16+.** A `CREATEROLE`-privileged creator
   holds *implicit ADMIN OPTION* on any role it just created, so the check
   returns `TRUE` before any real membership `GRANT` has run — the
   self-grant guard it protected was "always skipped, silently, in exactly
   the environment it needed to fire."
3. **`GRANT <role> TO CURRENT_USER` terminates the connection outright** on
   live Supabase — not denied, the connection dies mid-operation.
   Reproduced independently over both the Supavisor transaction pooler
   (6543) and the session pooler (5432).
4. **RLS `FORCE` only binds the table owner** — `app_role` (the request
   role) is never the table owner, so it's fully RLS-subject with plain
   `ENABLE` regardless; `postgres`/`service_role` already carry
   `rolbypassrls=TRUE`, making `FORCE` a no-op for them today. Relying on
   that would be fragile: without `BYPASSRLS`, `FORCE` would bind the 0004
   trigger and a not-yet-role-separated `/v1/me` with no GUC set, and the
   trigger's own `EXCEPTION WHEN OTHERS` would swallow the resulting RLS
   error into a silent sign-up lockout.

**Why:** These aren't preferences — they're the specific constraints that
forced the shape of migrations 0004 and 0005 below. Fact (2) killed an
earlier `pg_has_role`-based idempotency guard; fact (3) killed an earlier
design using a dedicated `landlord_sync_role` (see next entry); fact (4) is
the entire justification for ENABLE-not-FORCE.
**Where:** `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py`
(docstring "OWNERSHIP MODEL" / "LIVE ROLE FACTS"), `0005_app_role_and_rls.py`
(docstring "LIVE ROLE FACTS" / "WHY NO FORCE"); PRs #166, #167.

### 0004's `SECURITY DEFINER` pattern — postgres-owned, not app-role-owned

**What:** The three `auth.users` lifecycle trigger functions
(`handle_auth_user_created`, `handle_auth_user_email_updated`,
`handle_auth_user_soft_delete`) are `SECURITY DEFINER`, `SET search_path =
public, pg_temp`, owned by whichever role runs the migration (`postgres` on
live Supabase) — **not** a separately-provisioned app role.
**Why:** An earlier design used a dedicated `NOLOGIN landlord_sync_role`
with `ALTER FUNCTION ... OWNER TO landlord_sync_role`, but that failed live
with `must be able to SET ROLE "landlord_sync_role"`, compounded by facts
(2) and (3) above (the self-grant guard that would have protected the
ownership transfer was unsound, and the `GRANT ... TO CURRENT_USER` needed
to establish the role membership kills the connection). The fix removes
the dedicated role entirely and owns the functions by the migrating role
instead — matching Supabase's own documented `handle_new_user()` pattern.
Explicitly flagged as a deferred tradeoff: issue #15's "owned by a role
that can write `landlords` but is not the app's request role" criterion is
not satisfied today; deferred to #22.
**Where:** `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py`;
test: `apps/api/tests/test_migrations_0004.py::test_functions_owned_by_migrating_role_with_public_execute_revoked`;
PR #166.

### 0005's RLS — `ENABLE`, never `FORCE`

**What:** `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY` on all 13 multi-tenant
tables; `FORCE ROW LEVEL SECURITY` never appears in the migration.
**Why:** Directly downstream of fact (4) above — see that entry.
**Where:** `apps/api/migrations/versions/0005_app_role_and_rls.py`; test:
`apps/api/tests/test_migrations_0005.py::test_rls_enabled_not_forced_on_every_table`;
PR #167.

---

## 2. Role model & connection topology

**What:** `app_role` is created `NOLOGIN` (migration 0005), idempotent via
plain `pg_roles` catalog lookups — never `pg_has_role` (see §1). The flip
to a login-capable operator role is **manual, human, one-time, and never
in a migration**: an operator runs
`ALTER ROLE app_role LOGIN PASSWORD '<secret>';` directly against the live
database, then sets the `APP_DATABASE_URL` Fly secret and redeploys.
**Boot gates:** `app/config.py`'s `_require_app_database_url_in_production`
refuses to boot in `production` if `APP_DATABASE_URL` is unset (structurally
identical to `_require_public_base_url_in_production`, §4). Until the
operator step is done, request sessions silently fall back to the admin
engine with a one-time (per-process) warning log — a safe default only up
until real tenant data exists. A startup-time check,
`verify_request_engine_role_separation()`, queries both engines'
*server-reported* `current_user`/`rolbypassrls` and raises if the request
role has `rolbypassrls=True` or matches the admin engine's `current_user` —
comparing server-reported to server-reported, not to the client-side
`engine.url.username`, because under Supavisor the reported user is
`postgres.<project-ref>`, not bare `postgres` — a naive client-side
comparison would never match.
**GUC / `SET LOCAL` discipline:** `app/deps.py`'s `require_landlord` is the
*only* place allowed to set `app.current_landlord_id`, via parameterized
`set_config('app.current_landlord_id', :landlord_id, true)` — the `true`
argument is `is_local`, i.e. `SET LOCAL` semantics scoped to the current
transaction, never a plain `SET`. Enforced by a grep-based test
(`test_current_landlord_id_guc_only_set_in_deps_py`).
**The mid-commit warning:** `require_landlord`'s own docstring: *"a
mid-handler `await session.commit()` ends the CURRENT transaction — and
with it, this GUC... Any query the same handler runs AFTER that commit
executes on a new, unscoped transaction and fails closed to zero rows...
silent and confusing to debug, not an error. Do not call `session.commit()`
inside a handler that used `require_landlord`."*
**Where:** `apps/api/app/db/session.py`, `apps/api/app/config.py`,
`apps/api/app/deps.py`, `apps/api/migrations/versions/0005_app_role_and_rls.py`;
tests: `apps/api/tests/test_role_separation_check.py`,
`apps/api/tests/test_migrations_0005.py`; PRs #166, #167.

---

## 3. Append-only mechanics

**What — the REVOKEs:** `GRANT SELECT, INSERT ON messages, audit_log,
message_status_events TO app_role`, then `REVOKE UPDATE, DELETE ON
messages, audit_log, message_status_events FROM app_role`.
**Why now, not at table creation:** Migration 0002 (PR #148) shipped the
tables but *deferred* the REVOKE — the app role didn't exist yet in local
Postgres. The gate ("REVOKE must land before any writer ships") was
documented in the migration docstring and pinned by
`test_append_only_revoke_gate_documented`, with a safety-review note that
the gap was zero-exploitable-surface at the time (no writer to these
tables existed anywhere in `app/`). Migration 0005 is where the gate
actually closes.
**Where:** `apps/api/migrations/versions/0005_app_role_and_rls.py`;
deferral origin: `apps/api/migrations/versions/0002_core_schema.py`;
PRs #148, #167.

**What — the `twilio_status` deprecation:** `messages.twilio_status` is
dropped outright in migration 0003 (`ALTER TABLE messages DROP COLUMN
twilio_status`), superseded by the new append-only `message_status_events`
table.
**Why:** `messages` is append-only (never-break rule #2) and the row is
inserted by the webhook before any delivery status is known — writing
delivery status into it later would require an UPDATE on an append-only
table. Resolved the same way schema contradictions get resolved here: a
separate append-only event table instead of an in-place column.
**Where:** `apps/api/migrations/versions/0003_message_status_events.py`;
`docs/03-engineering/schema-v1.md`'s v1.1 amendments; PRs #153, #154.

**What — the `messages` classification/cost columns deprecation (v1.6)
and the `audit_log`-as-canonical-record pattern:** `messages.classification`
/ `tokens_in` / `tokens_out` / `model` / `llm_cost_cents` are DEPRECATED —
never written, columns stay listed until a future numbered DROP. The
canonical classification record is instead an `audit_log` row:
`actor='agent'`, `action='classified'`, `payload = {message_id, case_id,
severity, rules_fired, modifier, refusal_flags, model, tokens_in,
tokens_out, cost_cents, prompt_version}`. `draft_response` follows the
identical pattern for `action='drafted'`.
**Why:** Same contradiction class as `twilio_status` above, found during
#32's implementation: `messages` is append-only and the row is inserted
*before* classification ever runs, so populating those five columns after
the fact means an UPDATE. No migration was required — this is a doc-first
schema amendment (v1.6) applied the same PR it was found in.
**Where:** `docs/03-engineering/schema-v1.md`'s v1.6 amendments;
`apps/api/app/agent/nodes/classify_severity.py` (module docstring, "Canonical
classification record"; `_INSERT_CLASSIFIED_AUDIT_SQL`);
`apps/api/app/agent/nodes/draft_response.py` (module docstring, "Cost
accounting"); PR #175.

**What — `message_status_events` precedence rule:** Delivery state is
governed by strict status precedence — `failed/undelivered > delivered >
sent > sending > queued/accepted` (terminal states win; between terminals,
failure wins so a real failure is never masked) — recency is *never* the
criterion. No UNIQUE constraint, no upsert; duplicates are appended as
facts, resolved by a reader, not a writer.
**Why:** Twilio's status callbacks can arrive out of order or repeat; an
append-only fact log with read-side precedence resolution survives both,
where an upsert-by-recency would not. As of #171, the precedence
*resolution* logic is documented but not yet implemented in application
code — only the append + vocabulary CHECK exist; the webhook explicitly
defers reading-side precedence to a future queue/case-read consumer.
**Where:** `docs/03-engineering/schema-v1.md` (`message_status_events`
section); `apps/api/migrations/versions/0003_message_status_events.py`;
`apps/api/app/routers/webhooks/twilio.py` (status-callback handler); PRs
#153, #154, #171.

**What — the notifications dedupe index and never-delete invariant:** a
real Postgres partial unique expression index,
`uq_notifications_message_dedupe`, on `(payload ->> 'message_id', type)
WHERE type IN ('emergency_call', 'needs_eyes')`.
**Why:** Replaces an earlier app-level `WHERE NOT EXISTS` pre-check that
PR #171's review reproduced as unsafe 3/3 under genuine cross-process
concurrency — two concurrent Twilio redeliveries both pass the existence
check before either commits, producing duplicate emergency escalations. A
real unique index (with `ON CONFLICT (...) DO NOTHING`, matching the
index's expression/predicate verbatim, required for Postgres's
unique-index inference) closes the race at the database. The invariant
this depends on: rows of these two types must **never** be deleted — a
future retention/archival job must exclude them, documented identically in
both the migration and `schema-v1.md`.
**Where:** `apps/api/migrations/versions/0006_notifications_message_dedupe_index.py`;
consumer: `apps/api/app/routers/webhooks/twilio.py`
(`_INSERT_NEEDS_EYES_SQL` / `_INSERT_EMERGENCY_NOTIFICATION_SQL`); PR #171.

---

## 4. The webhook front door (`apps/api/app/routers/webhooks/twilio.py`)

**Commit-first persistence.** The inbound-SMS handler resolves the
property, runs the Tier-0 prefilter, resolves routing, then
`INSERT ... ON CONFLICT (twilio_sid) DO NOTHING RETURNING id` and commits
**that transaction immediately** — before any side effect (classification,
notifications) runs. Docstring: *"commit THIS transaction immediately...
Nothing that runs after this line can ever roll back the message row."*
Why: an earlier design ran side effects on the same session; a
caught-but-not-reraised side-effect failure poisoned the transaction, so
the final teardown commit silently rolled back — deleting the tenant's
message behind an already-returned 200. Reproduced by fault injection in
PR #171's review.

**Idempotent artifacts keyed on `message_id`.** Two keys: the message row
itself via `messages.twilio_sid`'s unique constraint
(`ON CONFLICT (twilio_sid) DO NOTHING`), and notifications via the §3
dedupe index (`ON CONFLICT ((payload ->> 'message_id'), type) ... DO
NOTHING`). NULL-`message_id` rows are unconstrained by design (Postgres
never treats two NULLs as equal).

**Recovery-5xx semantics.** The SMS webhook intentionally raises a 500
(`code="recovery_failed"`) when the conflict-path recovery SELECT fails,
finds no row, or the recovered `prefilter` snapshot fails to parse — so
Twilio retries. Contrast: the *status* callback webhook never returns 5xx
— its entire post-signature-verification body is wrapped in one
try/except that swallows to a bare 200, because a DB blip on a
fire-and-forget delivery callback becoming an unintended 500 is itself a
contract violation for Twilio's retry semantics on that endpoint.
**Where:** `apps/api/tests/test_webhooks_twilio_sms.py::test_recovery_select_failure_returns_5xx_then_retry_completes_artifacts`,
`apps/api/tests/test_webhooks_twilio_status.py::test_db_error_during_lookup_returns_200_not_500`.

**Signature verification + `public_base_url` gate.** `verify_signature`
(`app/integrations/twilio.py`) is HMAC-SHA1 per Twilio's documented
algorithm with a constant-time (`hmac.compare_digest`) comparison, checked
before any DB access. `public_base_url`, when configured, reconstructs the
signed URL independent of any request header
(`public_base_url.rstrip("/") + request.path[+query]`); unset, it falls
back to `X-Forwarded-Proto`/`X-Forwarded-Host` (safe only because Fly.io is
the single trusted proxy hop). Boot gate:
`app/config.py::_require_public_base_url_in_production` refuses to boot in
production without it set.

**HMAC-keyed digests.** `twilio.py::_digest` — `hmac.new(twilio_auth_token,
value, sha256).hexdigest()[:16]` — keyed with the Twilio auth token, used
to correlate repeated unrecognized `To` numbers in logs/Sentry without
exposing the real number. Why keyed, not plain `sha256(value)[:16]`: E.164
phone numbers are an enumerable keyspace, so an *unkeyed* truncated hash is
brute-forceable; keying with a secret makes the truncation safe. (This is a
log-correlation digest, not the status-callback authenticity check — that
is the same `verify_signature` HMAC-SHA1 used by both `/sms` and
`/status`.)
**Where:** `apps/api/app/routers/webhooks/twilio.py`,
`apps/api/app/integrations/twilio.py`, `apps/api/app/config.py`;
PR #171.

---

## 5. JWKS verification (`apps/api/app/integrations/supabase_auth.py`)

**`_JwksState`** — one internal state object holding: `cache` (jwks data +
monotonic fetch time), `lock` (`asyncio.Lock`), and three independent
cooldown stamps: `last_forced_refresh`, `last_degenerate_fetch`,
`last_fetch_exception`. `reset_for_tests()` replaces every field including
a **fresh** lock (reusing one across event loops raises under pytest-asyncio's
function-scoped loops).

**The three durable cooldowns:**
| Cooldown | Value | Path | Origin |
|---|---|---|---|
| `_FORCED_REFRESH_WINDOW_SECONDS` | 60s | kid-miss forced refresh | PR #146 |
| `_DEGENERATE_FETCH_COOLDOWN_SECONDS` | 60s | empty `{"keys": []}` response | PR #159 (split out from #146's originally-shared constant) |
| `_FETCH_EXCEPTION_COOLDOWN_SECONDS` | 5s | fetch raised an exception | PR #163 (deliberately shorter — "a transient network blip must not impose a minute of auth-down") |

`_JWKS_TTL_SECONDS = 86_400.0` (24h) is the base cache TTL, not a cooldown.
PR #163 also consolidated four separate module globals into the single
`_JwksState`.

**Stamp-timing semantics — two different, deliberate choices:**
- `last_forced_refresh` (kid-miss path) is stamped **before** the fetch
  attempt. Comment: "the window must bound attempts, not just successes."
  This fixes a real bug PR #146's safety review caught: stamping only on
  success meant a per-request upstream fetch attempt storm while the
  Supabase JWKS endpoint was down — a blocking finding.
- `last_degenerate_fetch` and `last_fetch_exception` (routine fetch path)
  are stamped **on observation** — only once the outcome is known — the
  opposite choice. PR #163's mutation-kill tests specifically pin
  stamp-on-observation for these two.

**Refetch-once-on-kid-miss.** `verify_jwt` catches an `AuthError` from
`_find_signing_key`, calls `_refresh_jwks_on_kid_miss()`, then retries
`_find_signing_key` exactly once more — no loop around the second call.
Guarded against DoS/infinite refetch by the 60s `_FORCED_REFRESH_WINDOW_SECONDS`
rate limit and coalesced under `_jwks_state.lock` for concurrent kid-misses.
**Why:** survive key rotation (a `kid` legitimately absent from the cached
set) without a per-request refetch storm on a genuinely bad/expired token.
**Where:** `apps/api/app/integrations/supabase_auth.py`; PRs #146, #159, #163.

**Related fixes in the same subsystem, for provenance:**
- **PR #157** — two flaky-401 mechanisms, both test-only bugs (zero diff
  to `app/`): a respx mock-nesting bug (fixed by wrapping one shared
  `MockRouter` around a `gather` rather than one per coroutine), and a JWK
  coordinate-width bug in the *test helper* that derived EC coordinate byte
  length from `n.bit_length()` instead of a fixed 32 bytes — producing a
  one-byte-short encoding on ~0.78% of generated keypairs and a spurious
  `invalid_token`. A real RFC 7518 §6.2.1 bug, but in test tooling, not
  production code (`supabase_auth.py` never constructs JWKs itself, only
  parses them).
- **PR #161** — phone-only signups get a clean `403 email_required`, not a
  500. Two-part fix: `verify_jwt` normalizes GoTrue's `"email": ""` claim
  (phone-only signups) to `None` rather than leaving it a falsy-but-truthy
  string; `app/routers/me.py` checks `if not user.email` *before any DB
  call* — closing a more severe bug the safety review caught, where a naive
  `is None` check alone would have let `""` sail into the upsert and
  silently overwrite an existing landlord's stored email on a plain GET.
- **PR #164** — request IDs get a `req_` prefix
  (`app/middleware/request_id.py::_generate_request_id` →
  `f"req_{uuid.uuid4().hex}"`), feeding the error envelope's `request_id`
  per `api-contracts.md`. A well-formed client-supplied `X-Request-ID` is
  honored and echoed as-is, unprefixed.

---

## 6. LangGraph agent subsystem

### Checkpointer — dedicated `langgraph` schema

**What:** `AsyncPostgresSaver`'s four tables (`checkpoints`,
`checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) live in a
dedicated `langgraph` Postgres schema, `REVOKE`d from `PUBLIC`, `app_role`,
and (guarded) `anon`/`authenticated`, with `ALTER DEFAULT PRIVILEGES`
locking down future tables too.
**Why:** `apps/api/tests/test_rls_isolation_matrix.py`'s
`test_no_tables_outside_descriptor_set_exist_in_public_schema` (#23)
requires every table in `public` to either carry an RLS policy or be
excluded via a non-`public` schema. Putting checkpoint tables in a
dedicated schema keeps that test green *by construction*, with no new
RLS-policy/allowlist entry required — rather than teaching the RLS
inventory test about four tables LangGraph itself owns and migrates.
One checkpoint thread per **case**, keyed on `cases.langgraph_thread_id`.
**Where:** `apps/api/app/agent/checkpointer.py`;
`apps/api/migrations/versions/0007_langgraph_checkpoint_schema.py`; PR #172.

### Case lifecycle (`apps/api/app/agent/case_lifecycle.py`, migration 0008)

**`pending_resolved_at` — apply-at semantics, not proposal-time.**
`propose_resolution()` sets `pending_resolved_at = now() +
RESOLUTION_PROPOSAL_WINDOW` (48h) directly — the column stores *when the
resolution applies*, not when it was proposed. Why: a self-describing
column name, a trivial `pending_resolved_at <= now()` sweep predicate with
no arithmetic to duplicate anywhere it's read, and a future window-length
change needs no migration. Mirrors the repo's existing "the undo window is
data, not a sleep" precedent (`drafts.scheduled_send_at`).

**Self-guarding sweep writes.** A safety review proved a live TOCTOU: the
sweep read case state, decided an action, then wrote — racing a concurrent
event between the read and the write. Fixed with per-leg UPDATEs whose
`WHERE` clause **re-asserts the same condition** the decision was made
under (e.g. `WHERE id = :case_id AND pending_resolved_at IS NOT NULL AND
pending_resolved_at <= :now`), gated on `rowcount == 1`; a miss logs
`case_lifecycle_sweep_guard_miss` and silently no-ops rather than forcing
a write that's no longer valid.

**`awaiting_approval` exclusion.** `AUTO_STALE_ELIGIBLE_STATUSES` is every
open status *except* `awaiting_approval`. Why: a case sitting in
`awaiting_approval` has a draft the landlord hasn't acted on yet —
auto-staling it out from under them would silently erase their own
backlog.

**Auto-stale → `needs_eyes`, distinct from resolving.** The auto-stale sweep
leg inserts a `notifications` row (`type='needs_eyes'`, `channel='push'`,
`status='pending'`) alongside the resolution — closing a case with nothing
surfaced to the landlord is exactly the failure this exists to prevent.

**Precedence over the 14-day auto-stale sweep.** A case with
`pending_resolved_at IS NOT NULL` is never auto-staled regardless of
`last_activity_at` age — the pending, tenant-confirmed signal is more
specific and more recent than mere inactivity.
**Where:** `apps/api/app/agent/case_lifecycle.py`;
`apps/api/migrations/versions/0008_cases_pending_resolved_at.py`; PR #173.

### Degraded-path drafts are approvable/rejectable via the SAME endpoints (#44-pinned decision, closed)

**What:** A pending draft inserted by `draft_response`'s degraded-mode exit
(an LLM-classified EMERGENCY, or the draft's own hard-guard failing twice)
never actually pauses behind a live `interrupt()` — `_route_after_draft_
response` routes straight to `degraded_mode -> END` instead of `mark_
awaiting_approval -> await_approval`, so `cases.status` never becomes
`awaiting_approval` and no LangGraph interrupt is ever created for that
draft. #43 (the shadow-mode pause) left this open, pinned on issue #44 for
whoever built the approve/reject endpoints to decide. **Decided: these
drafts ARE approvable/rejectable/editable from the dashboard, via the
identical four endpoints** — a landlord must not lose the ability to act
on a safe-fallback or EMERGENCY-drafted reply just because the interim
degraded-mode routing never paused it.
**Why:** Implemented as a sanctioned SECOND path
(`_finalize_never_paused_draft`), never by loosening `resume_case_thread`
itself (which would risk the double-resume hazard its own concurrency
tests pin). `resolve_draft_decision` (the actual entry point
`routers/drafts.py` calls) picks between `resume_case_thread` (a live
interrupt genuinely exists) and the non-graph fallback (never paused) by
peeking `cases.status` — `awaiting_approval` implies a live interrupt
UNLESS a later message's own degraded-mode exit drained it while leaving
`cases.status` untouched (the "trap," a distinct, separately safety-fixed
finding — see this entry's own follow-up below). Both paths call the
IDENTICAL write helpers (`apply_approve_or_edit`/`apply_rejection`) under
the same per-case advisory lock, so there is exactly one place that ever
marks a draft `approved`/`rejected`, regardless of entry path.
**The trap (safety review, follow-up round):** `cases.status ==
'awaiting_approval'` alone is not proof a live interrupt exists — a case
can sit at `awaiting_approval` (set by an EARLIER message) while a LATER
message's own fresh re-run on the SAME thread drains the interrupt via its
OWN degraded-mode exit, without ever revisiting `cases.status`. Naively
re-raising `CaseNotAwaitingApprovalError` here (as the genuine
concurrent-resume race correctly does) would 409 `draft_stale` with
`fresh_draft_id` equal to the very id the caller just submitted — an
infinite, permanently-unsendable retry loop. Fixed: `resolve_draft_
decision` distinguishes this case (the draft is STILL pending) from the
genuine race (the draft is NO LONGER pending — a concurrent winner already
resolved it) and falls through to the same non-graph fallback only for the
former.
**Where:** `apps/api/app/agent/graph.py` (`resolve_draft_decision`,
`_finalize_never_paused_draft`, `CaseNotAwaitingApprovalError`'s own
docstring — causes 1/2/3); `apps/api/app/agent/nodes/finalize_draft_
decision.py`; tests:
`apps/api/tests/test_agent_finalize_draft_decision.py` (`test_degraded_
path_pending_draft_is_approvable_via_fallback`, `test_trap_awaiting_
approval_case_drained_interrupt_still_approvable`); issue #44 (comment
closing the pin).

---

## 7. The LLM layer

**"No wrapper" means no framework hides the prompt — not literal absence
of shared code.** `architecture.md` / `apps/api/CLAUDE.md` both described
`classify_severity` as calling "the Anthropic SDK directly ... (not a
wrapper)"; literally, `integrations/anthropic.py`'s `call_tool_forced` is a
thin wrapper around `client.messages.create`. Spec-guardian ruling
(2026-07-05): the intent behind "no wrapper" is that no framework may hide
the rubric prompt or let a node delegate its *own* prompt-construction or
tool-choice decisions to shared code — every node still builds its own
system/user content and picks its own tool verbatim; `call_tool_forced`
owns transport only (the `asyncio.wait_for` timeout, forced
`tool_choice`/`tools` plumbing, response parsing) — never a prompt or
classification decision. Code stands as written; this entry (plus the
Deliverable-2 wording fix in `architecture.md`/`CLAUDE.md`) is the "future
docs pass" the module docstring flagged as still owed.
**Where:** `apps/api/app/integrations/anthropic.py` (module docstring,
"Reported gap: 'direct SDK, no wrapper' vs. this module").

**Temperature is deprecated on `claude-sonnet-5`.** Issue #32 specified
`temperature=0`; the live API rejects any request that sets it at all
(400, `` `temperature` is deprecated for this model ``). The parameter is
omitted entirely — the determinism the spec intended is instead enforced
by the eval discipline (3 samples/scenario, flaky = fail; §8). Flagged in
code as a live-smoke finding, not silently patched over — it's a real,
acknowledged gap against `apps/api/CLAUDE.md`'s older "temperature 0"
wording (fixed by Deliverable 2 of this same doc pass).
**Where:** `apps/api/app/integrations/anthropic.py`
(`call_tool_forced`'s "DETERMINISM NOTE").

**The 20s end-to-end budget ruling.** `docs/02-product/emergency-prefilter.md`'s
"classification budget: 20 seconds end-to-end" governs literally: **one**
shared 20-second deadline covers the initial attempt *and* its single
retry *together* — never 20 seconds per attempt (an earlier revision
misread it as per-attempt; corrected per a spec-guardian ruling). The
first attempt is capped at 12s even if the full budget remains, so one
slow-but-not-timed-out call can't silently consume the entire deadline; the
retry gets whatever remains and is skipped entirely (treated as a second
failure) if that remainder is below a 2s floor — never attempted with a
near-zero timeout.
**Where:** `apps/api/app/integrations/anthropic.py`
(`CLASSIFICATION_BUDGET_SECONDS`, `FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS`,
`MIN_RETRY_BUDGET_SECONDS`, `new_deadline`/`attempt_timeout`); consumed
identically by `classify_severity.py` and `draft_response.py`.

**The Tier-0 clamp.** `app/agent/prefilter.py`'s `PrefilterResult.hard_hit`
(snapshotted onto `messages.prefilter` by the webhook *before* the graph
ever runs) is the one thing `classify_severity` is never allowed to
override downward: if the prefilter fired and the model's own severity
came back as anything other than `EMERGENCY`, the node clamps the severity
to `EMERGENCY`, records the clamp in `rules_fired`, and appends the
mandated reasoning-log line verbatim ("The alarm phrasing already made
this an emergency — I kept it there."). The reverse direction — escalating
past a Tier-0 miss — is always allowed, no special-casing. The clamp reads
the durable DB snapshot, never the in-memory `AgentState`, so it survives
process boundaries.
**Where:** `apps/api/app/agent/nodes/classify_severity.py`
("The Tier-0 clamp"); `apps/api/app/agent/prefilter.py`; PR #175.

**Code-appended deferrals, never model-generated.** An earlier revision
asked the model to weave refusal-deferral language into its own reply
naturally — but the frozen system prompt's "concise, no boilerplate"
guidance pushed against verbatim reproduction, so a model that
*paraphrased* a flagged topic still carried enough vocabulary to trip its
own guard, degrading a safe reply to the generic fallback for wording
alone. Fixed by separating drafting from deferral entirely: the model is
told not to address a flagged topic beyond one neutral sentence and never
to write deferral language itself; this node (`_append_deferrals`) appends
the canned `REFUSAL_TEMPLATES` text verbatim, by construction, after the
model's acknowledgment is accepted or replaced — the model never generates
what needs to be exactly right.
**Where:** `apps/api/app/agent/nodes/draft_response.py`
("Refusal-deferral templates: code APPENDS"); PR #175.

**Guards as a v1 backstop; the eval grader is authoritative.** The
post-generation hard guards (dollar-amount/compensation commitments, access
codes, legal positions) are deterministic substring/regex checks, not
semantic understanding — a fast, always-on backstop that catches a
violation before a landlord ever sees the draft. `#35`'s eval grader
(LLM-as-judge + its own substring assertions) is explicitly documented as
the *authoritative* check for whether a draft actually violated a hard
rule; the guards are the first line of defense that ships with this issue,
not a replacement for that grader.
**Where:** `apps/api/app/agent/nodes/draft_response.py` ("v1
pattern-coverage, not the authoritative gate"); PR #175.

**`max_retries=0` on the Anthropic client.** The SDK's own hidden default
is 2 retries with backoff on retryable errors — layered *underneath* the
repo's own retry-once budget arithmetic (previous entry), which would
silently triple the worst-case round-trips per attempt (up to 3 real HTTP
calls, invisible to the `asyncio.wait_for` budget that only ever awaits
once per attempt). The budget arithmetic must own all retry behavior
end-to-end; the SDK must not retry beneath it.
**Where:** `apps/api/app/integrations/anthropic.py` (`get_client`).

**Cost accounting lives in `audit_log` payloads, not `messages`.**
`estimate_cost_cents` uses a small, hardcoded, deliberately conservative
pricing table ($3.00/MTok in, $15.00/MTok out — Anthropic's long-standing
Sonnet-tier rate, not independently confirmed for `claude-sonnet-5`
specifically — a placeholder to reconcile once real billing data exists).
Every real Anthropic call (including a rejected-and-regenerated attempt in
`draft_response`) contributes `tokens_in`/`tokens_out` to a running total;
`model`, `tokens_in`, `tokens_out`, and `cost_cents` are written into the
`audit_log` `'classified'` and `'drafted'` payloads respectively — see §3's
v1.6 entry for why not the `messages` columns.
**Where:** `apps/api/app/integrations/anthropic.py`
(`estimate_cost_cents`, `_INPUT_PRICE_PER_MTOK_USD`/`_OUTPUT_PRICE_PER_MTOK_USD`);
`apps/api/app/agent/nodes/classify_severity.py`,
`apps/api/app/agent/nodes/draft_response.py`.

---

## 8. The eval harness (`apps/api/evals/`)

**3 samples, flaky = fail — a stricter bar under no-temperature, not a
weaker one.** `CLASSIFICATION_SAMPLES_PER_SCENARIO = 3`; every sample must
independently pass its own assertions against `expect`, or the whole
scenario fails (`ScenarioResult.classification_ok`: `all(sample.ok for
sample in self.classification_samples)`). **Why:** since `temperature`
can't be pinned to 0 anymore (§7), the model's own natural sampling
variance is now the exact thing being policed — the runner's module
docstring calls this out directly: *"a 'flaky' result here does not
necessarily mean the harness or prompt has a bug"* — the rule's authors
likely pictured a weaker bar (pin `temp=0`, run once) than what actually
ships today.
**Where:** `apps/api/evals/runner.py`
(`CLASSIFICATION_SAMPLES_PER_SCENARIO`, module docstring "Why 3 samples +
no temperature is a STRICTER bar"), `apps/api/evals/scoring.py`
(`ScenarioResult.classification_ok`); `docs/02-product/eval-scenarios-v1.md`
("Run matrix per scenario").

**Token-budget pacing — per call type, not per severity tier.** The
harness paces its own request rate against Anthropic with
`EVAL_TOKEN_BUDGET_PER_MIN = 25_000` and per-tool input-token estimates
(`classify_severity`: 4500, `draft_message`: 2000, `judge_draft`: 1500). A
routine and an emergency scenario get identical pacing — only *which LLM
call* is being made (classify vs. draft vs. judge) changes the estimate,
not the scenario's severity tier.
**Where:** `apps/api/evals/runner.py` (`EVAL_TOKEN_BUDGET_PER_MIN`,
`_ESTIMATED_INPUT_TOKENS_BY_TOOL`).

**Infra-error vs. wrong-answer distinction.** A `ScenarioInfraError`
(rate-limit/overload backoff exhaustion, or any `AnthropicCallError`/
`ValidationError`) is a bucket disjoint from a graded hard/soft failure —
*"an errored scenario was never actually graded... 'inconclusive' is not
'confirmed wrong.'"* The release gate still blocks on either a confirmed
hard failure **or** any errored scenario (infra flakiness stops a release
too), but the two are never conflated inside the scoring itself.
**Where:** `apps/api/evals/runner.py` (`ScenarioInfraError`,
`run_scenario`), `apps/api/evals/scoring.py` (`ScenarioResult.errored`,
`GateVerdict.errored_scenario_ids` / `release_blocked`).

**The harness merged green — PR #177, gate 9 = 20/20.** The gate arc that
got there: 14/20 → 19/20 (judge verdict inversion fixed at three layers —
when a judge fails a draft, ALWAYS cross-check its prose reasoning against
its boolean checklist before believing either; disagreement = eval-infra
bug, not product bug) → 18/20 (f1 root-caused to the frozen v1
`legal_rent_ltb` template's own 29-word legalistic copy, which
`plain-language-rules.md` binds) → **prompts v2** → 18/20 (f1 failed on the
single word "soon", rule 4 concrete-over-relative; e4 hit a third
output-shape variance) → **20/20, `release_blocked=false`**, baseline
committed as `evals/results/v1-baseline.json` (the one sanctioned
`.gitignore` exception).
**Where:** PR #177; `apps/api/evals/results/v1-baseline.json`.

**Prompts v2 — a templates-only version bump, founder-approved
2026-07-06.** The eval judge repeatedly failed v1's refusal-template copy
against the product's own plain-language rules; fixing customer-facing
template text is a prompt version bump (human-gated). `prompts/v2.py`
changes ONLY `REFUSAL_TEMPLATES` (legal_rent_ltb + impersonation rewritten,
access_codes + cost_compensation plained, other_tenants byte-identical);
the classify/draft system prompts are **re-exported from v1 by reference**,
so they cannot drift from what the baseline measured — pinned by an
`is`-identity test. No template makes a time commitment: a concrete time in
a refusal deferral would be a false commitment on the landlord's behalf.
**Where:** `apps/api/app/agent/prompts/v2.py`;
`apps/api/tests/test_agent_schemas.py`
(`test_prompts_v2_changes_exactly_the_founder_approved_templates`).

**Model-output shape coercions fail CLOSED — the boolean-modifier
ruling.** Three observed variance classes are absorbed at the schema
boundary (single-key wrapper; `refusal_flags` as a per-flag boolean dict;
an invented `vulnerable_occupant_modifier_applied` bool). Safety review
caught the first absorb of the third class being fail-open: `modifier`
never re-derives severity, so accepting `ROUTINE` + `true` would have
turned "validation error → retry → `classification_failed` → landlord
notification" into a silent under-classification on the one path where
silence kills. Ruling: `False` is absorbed (asserts nothing); `True` is
absorbed only when severity is already EMERGENCY; anything else raises.
Companion trap: Pydantic executes multiple `mode="before"` model
validators in REVERSE definition order — compose shape normalizations into
ONE validator with explicit sequencing, and keep a composition test.
**Where:** `apps/api/app/agent/schemas.py` (`_unwrap_wrapper` docstring),
`apps/api/tests/test_agent_schemas.py`
(`test_severity_result_boolean_modifier_true_below_emergency_fails_closed`,
`test_severity_result_wrapper_plus_gate8_variances_compose`).

**Bare `pytest` can never spend money.** `[tool.pytest.ini_options]`
carries `addopts = "-m 'not eval'"` (senior review, PR #177): the default
command structurally cannot reach the paid gate; an explicit CLI `-m eval`
still overrides it (last `-m` wins), which is how the gate is deliberately
run — by the orchestrator, with founder authorization, never unilaterally.
**Where:** `apps/api/pyproject.toml` (`addopts`).

**The E2 catch — the harness's origin story, precisely attributed.**
Building the harness surfaced a real Tier-0 false negative:
`e2-gas-smell`'s canonical message ("the kitchen has smelled like gas
since I got home an hour ago") never fired the prefilter's gas/CO
proximity trigger, because its word list never included the past tense
"smelled." Labeled in code as a "#144-class defect" — the same
hand-copied-word-list-missing-an-inflection pattern PR #144 fixed — but
this is a distinct, follow-on discovery, not part of #144's own diff: the
fix landed through the harness build-out itself, tracked as a strict
`xfail` until closed.
**Where:** `apps/api/tests/test_evals.py` ("FORMERLY-DISCOVERED DEFECT,
NOW FIXED"); `apps/api/app/agent/prefilter.py` ("Tense/inflection-
completeness sweep — post-#144, found building the #35/#36 eval harness");
`docs/02-product/eval-scenarios-v1.md` (`e2-gas-smell`).

---

## 9. Testing conventions

**respx: capture inside the context, never nest.** A single
`respx.MockRouter` is opened once and wraps the *entire* operation under
test, including concurrent branches (`asyncio.gather`) — never one mock
context per coroutine. Nesting caused a real flake (#145): overlapping
contexts could unmock a fetch mid-gather, producing a spurious 401 against
a cold JWKS cache. Stated as a repo-wide rule: "we never nest respx
contexts."
**Where:** `apps/api/tests/test_me.py`
(`test_me_concurrent_first_calls_no_duplicate_key`), `apps/api/tests/test_auth.py`;
PR #157.

**`_now()` seams.** Three integration modules
(`supabase_auth.py`, `anthropic.py`, `weather.py`) each define a private
`_now()` — a thin wrapper around `time.monotonic()` that tests monkeypatch
to simulate elapsed time without a real `sleep()`. Each module's docstring
cross-references the others as "the same pattern"; the eval harness's own
pacing sleep copies the identical idiom.
**Where:** `apps/api/app/integrations/{supabase_auth,anthropic,weather}.py`;
`apps/api/tests/{test_auth,test_integrations_anthropic,test_weather}.py`;
`apps/api/evals/runner.py`.

**Mutation-kill discipline.** An explicit, numbered practice, not informal
hardening — tests state which mutant they kill in their own docstrings
(e.g. "swap stamp-on-observation for stamp-on-attempt," "swap the fixed
role comparison for the old client-side one") and tie it to a specific
spec-guardian finding. PR #168's own test plan lists "mutation-verified red
states": drop one RLS policy, disable `rowsecurity`, etc. — the isolation
matrix and its catalog gate must go red for each.
**Where:** `apps/api/tests/test_auth.py` (tests 35/36 — fetch-exception
cooldown stamp-timing), `apps/api/tests/test_role_separation_check.py`,
`apps/api/tests/test_migrations_0004.py`, `apps/api/tests/test_rls_isolation_matrix.py`;
PR #168.

**`SET LOCAL ROLE` for RLS tests — a transaction-scoped SQL constant, not a
pytest fixture.** `_SET_ROLE_APP_ROLE_SQL = text("SET LOCAL ROLE
app_role")`, executed inside a connection's own transaction (begin → `SET
LOCAL ROLE` → set the landlord GUC → query → rollback). **Why:** `app_role`
is `NOLOGIN` by design (§2); a superuser can `SET LOCAL ROLE` to it without
membership, scoped to the transaction and auto-reverting on rollback —
replacing an earlier password-based `ALTER ROLE ... LOGIN` design that
could leak a `rolcanlogin=true` role if a test was interrupted mid-run.
**Where:** `apps/api/tests/test_rls_isolation.py`,
`apps/api/tests/test_rls_isolation_matrix.py`; PR #168.

**Divergence-lever sentinels instead of comparing a frozen
`transaction_timestamp()`.** A same-value-UPDATE test (does the
`auth.users` trigger correctly no-op when the email didn't actually
change?) can't be verified by comparing `updated_at` before/after within
one transaction — Postgres's `now()` is `transaction_timestamp()`, frozen
for the whole transaction, so that comparison is a tautology that stays
green even with the guard removed (verified empirically). Fixed by
planting a sentinel via a direct `UPDATE` that bypasses the trigger, then
firing the same-value `UPDATE` through the trigger path: if the `WHEN
(NEW.email IS DISTINCT FROM OLD.email)` guard holds, the sentinel
survives; if the trigger fires anyway, it clobbers the sentinel back.
**Where:** `apps/api/tests/test_migrations_0004.py`
(`test_update_email_to_same_value_does_not_refire_trigger`).

**Regression tests are named per finding, not a generated base-vs-head
matrix.** `apps/api/tests/test_prefilter.py`'s durable convention is one
test class per specific safety-review finding, named by issue/PR number
(`TestRegressionIssue143NineOneOneNormalization`,
`TestRegressionPr144SoundingContinuousAlarm`,
`TestRegressionRound3ContinuousSynonymFamily`, …), plus a small
`TestRegressionMustNotRegress` checklist. PR #144's description separately
references a "base-vs-head matrix" the safety-reviewer ran as ad hoc due
diligence during review (zero emergency regressions vs. `main`) — that
comparison was a review-time tool, not a committed pytest artifact; the
per-finding class convention above is what actually ships and stays green
over time.
**Where:** `apps/api/tests/test_prefilter.py`; PR #144.
