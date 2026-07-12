---
name: stoop-architecture-contract
description: >
  Load this skill before touching any load-bearing part of the Stoop backend
  (apps/api) or reasoning about its design: the Twilio SMS webhook, the Tier-0
  emergency prefilter, the LangGraph agent nodes (classify_severity,
  draft_response, etc.), the drafts/approval queue, RLS role separation and the
  admin-session escape hatch, migrations 0001-0008, append-only tables, the
  case lifecycle (reopen / auto-stale / pending_resolved_at sweeps), JWKS auth,
  the LLM time budget, or the LangGraph checkpointer. Also load it when asked
  "how does Stoop work", "what invariants must I not break", "why is there no
  send-to-tenant code", "why is the graph not wired", "is the dashboard real",
  or before writing/reviewing any PR that adds an endpoint, a migration, a
  node, or a Twilio/Anthropic call. It states the system shape, every numbered
  invariant with its enforcing artifact, the case-lifecycle contract, and the
  known-weak points (items 1–2 updated 2026-07-12; rest as of 2026-07-05).
---

# Stoop architecture contract — load-bearing design, invariants, weak points

Repo root: the Stoop monorepo. Backend = `apps/api` (Python 3.12 / FastAPI /
async SQLAlchemy 2.0 / LangGraph / Alembic, managed with `uv`). Docs in
`docs/` are the source of truth; code follows docs, never vice versa.

## When NOT to use this skill

| You actually need | Go to sibling skill |
|---|---|
| How to ship a change (gates, reviewers, never-break rules with incident history) | `stoop-change-control` |
| Symptom → triage for a failure you are seeing right now | `stoop-debugging-playbook` |
| Full incident history with root causes and evidence | `stoop-failure-archaeology` |
| Rubric doctrine, Ontario tenancy context, Supabase platform facts | `stoop-domain-reference` |
| Every config axis, defaults, how to add an env var | `stoop-config-and-flags` |
| Recreate the dev environment / environment traps | `stoop-build-and-env` |
| Run, migrate, operate; the `app_role` LOGIN operator flip | `stoop-run-and-operate` |
| What counts as test/eval evidence | `stoop-validation-and-qa` |
| The plan to close the Train-1 core loop (#34 → #111) | `stoop-core-loop-campaign` |

## System shape in 15 lines

```
Tenant SMS ──► POST /webhooks/twilio/sms  (apps/api/app/routers/webhooks/twilio.py)
   1. verify Twilio HMAC signature (403 before any DB access)
   2. Tier-0 prefilter runs SYNCHRONOUSLY in the handler (app/agent/prefilter.py::check —
      pure function, no I/O; "Tier-0" = deterministic keyword emergency filter)
   3. INSERT message + prefilter snapshot, COMMIT FIRST (its own transaction, before any
      side effect); duplicate SID → recover existing row; recovery failure → 5xx (Twilio retries)
   4. idempotent side effects, each in an isolated session, deduped by Postgres unique index
   5. BackgroundTasks → app/agent/graph_entry.py::enqueue_classification — runs the REAL
      graph since PRs #185/#187 (as of 2026-07-12): app/agent/graph.py compiles the
      StateGraph under a per-case pg_advisory_xact_lock; pre-routing segment
      identify_property → load_context → identify_case, then classify_intent →
      classify_severity → conditional(draft_response → mark_awaiting_approval →
      await_approval interrupt() | degraded_mode); resume + interrupt contract: A23
drafts queue (drafts table, one pending per case) ──► landlord approval (#44/#45, unbuilt)
   ──► send to tenant/vendor: UNBUILT BY DESIGN — no Twilio REST/send call site exists
       anywhere in app/ (webhooks are inbound-only; sending arrives with #108/#45)
Runtime: FastAPI + async SQLAlchemy + Supabase Postgres via Supavisor transaction pooler
(port 6543); LangGraph checkpointer on its own psycopg pool in the `langgraph` schema.
```

Definitions used below: **RLS** = Postgres Row-Level Security. **GUC** = a
Postgres session/transaction-scoped setting (`current_setting(...)`), here
`app.current_landlord_id`. **Seam** = an honest stub with exactly one call
site, left for the issue that owns the real body. **Supavisor** = Supabase's
transaction-mode connection pooler.

## Numbered invariants — each with WHY and the enforcing artifact

Do not weaken any row without the change-control process (`stoop-change-control`).
Paths are repo-relative; migrations live in `apps/api/migrations/versions/`.

| # | Invariant | Why it exists | Enforcing artifact |
|---|---|---|---|
| 1 | `messages`, `audit_log`, `message_status_events` are append-only — no UPDATE/DELETE anywhere | The audit trail and message record must be tamper-proof (never-break rule #2); an UPDATE path also invites transaction-rollback message loss | `0005_app_role_and_rls.py`: `REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events FROM app_role`; grants asserted in `apps/api/tests/test_migrations_0005.py` |
| 2 | One pending draft per case | The approval queue shows exactly one actionable card per case; a new inbound marks the old draft `stale` and re-runs from `load_context` (stale-draft rule, `apps/api/CLAUDE.md`) | Partial unique index `uq_drafts_one_pending ON drafts (case_id) WHERE status = 'pending'` (`0002_core_schema.py`) |
| 3 | Tier-0 clamp: the agent may escalate past a Tier-0 miss, but may NEVER de-escalate a Tier-0 fire | A keyword-certain emergency ("FIRE") must not be talked down by an LLM | `app/agent/nodes/classify_severity.py`: reads the DURABLE `messages.prefilter` snapshot (written by the webhook, never recomputed), clamps to EMERGENCY, records the clamp in `rules_fired`, appends the verbatim line "The alarm phrasing already made this an emergency — I kept it there.", logs `classify_severity_tier0_clamp` |
| 4 | The undo window is data, not a sleep | Approve = set `scheduled_send_at = now()+5s`; the sender (unbuilt, #44/#45) only sends `approved` rows whose time has come — survives restarts, needs no in-process timer | `drafts.scheduled_send_at` column (`0002_core_schema.py`); rule stated in `apps/api/CLAUDE.md` |
| 5 | Canonical agent record = `audit_log` rows `action='classified'` / `action='drafted'` — NOT the `messages` columns | `messages` is append-only and the row is inserted by the webhook BEFORE classification runs; writing `messages.classification/tokens_in/tokens_out/model/llm_cost_cents` would require an UPDATE. Those five columns are DEPRECATED, never written | `docs/03-engineering/schema-v1.md` v1.6 amendments (inline "DEPRECATED v1.6" column comments); audit INSERTs in `app/agent/nodes/classify_severity.py` and `app/agent/nodes/draft_response.py` |
| 6 | `get_admin_session` (bypasses RLS) is allowlist-only | Every RLS bypass must be a deliberate, reviewed decision; the function body is deliberately a textual near-duplicate of `get_session` so `grep get_admin_session app/` is a complete honest list | `apps/api/tests/test_migrations_0005.py::test_get_admin_session_referenced_only_by_allowlisted_files` greps `app/` and red-fails on any new referencing file not in `_ADMIN_SESSION_ALLOWLIST` |
| 7 | Engine split: admin engine (`DATABASE_URL`) vs request engine (`APP_DATABASE_URL`/`app_role`); the landlord GUC is set ONLY in `app/deps.py::require_landlord` via `set_config('app.current_landlord_id', :id, true)` (i.e. `SET LOCAL`); NEVER `await session.commit()` mid-handler | RLS policies key off the GUC (`0005`: `USING (landlord_id = current_setting('app.current_landlord_id', true)::uuid)`). `SET LOCAL` dies with the transaction — a mid-handler commit silently unscopes every later query in that handler | `app/deps.py` (`_SET_CURRENT_LANDLORD_SQL` + the explicit "WARNING for #53-57 authors" block); `app/db/session.py` two-engine layout; `apps/api/tests/test_require_landlord.py` |
| 8 | Boot gates fail closed | Misconfigured safety infrastructure must abort startup, not degrade silently | `app/config.py`: production refuses to boot without `APP_DATABASE_URL` (`_require_app_database_url_in_production`) and without `PUBLIC_BASE_URL` (`_require_public_base_url_in_production`, so Twilio signature checks never trust proxy headers in prod). `app/main.py` lifespan: `verify_request_engine_role_separation()` (compares SERVER-reported `current_user` of both engines — never the client-side connection-string username, which differs under Supavisor — and rejects a `rolbypassrls` request role) then `setup_checkpointer()`; either raising aborts startup |
| 9 | LangGraph checkpointer lives in a dedicated `langgraph` schema, admin-engine-only, never RLS | The public-schema RLS inventory test (`test_no_tables_outside_descriptor_set_exist_in_public_schema`, `apps/api/tests/test_rls_isolation_matrix.py`) requires every `public` table to have a descriptor + RLS policy; keeping checkpoint tables out of `public` keeps that test green BY CONSTRUCTION | `0007_langgraph_checkpoint_schema.py` (`CREATE SCHEMA langgraph` + REVOKE from PUBLIC/`app_role`/`anon`/`authenticated`); `app/agent/checkpointer.py` (psycopg pool: `prepare_threshold=None` — never 0, which means prepare-everything — `autocommit=True`, `search_path` pinned twice: startup-packet `options` AND pool `configure` callback) |
| 10 | Webhook = commit-first + Postgres-enforced idempotent artifacts; recovery failures 5xx | Round 1 of the #40 safety review found silent message loss (one shared transaction, poisoned by a caught side-effect failure, rolled back the message INSERT under a 200); round 2 found cross-process duplicate emergency escalations in the app-level `WHERE NOT EXISTS` fix. Twilio's at-least-once retry IS the recovery mechanism — a 200 forecloses it | `app/routers/webhooks/twilio.py` (module docstring is the written contract; commit immediately after the message INSERT; each side effect in its own `_isolated_session`); `0006_notifications_message_dedupe_index.py`: partial unique index `uq_notifications_message_dedupe ON ((payload->>'message_id'), type) WHERE type IN ('emergency_call','needs_eyes')`; alerts and the audit row are gated on the `ON CONFLICT ... DO NOTHING RETURNING id` row actually returning |
| 11 | JWT verification is JWKS asymmetric-only (ES256/RS256), never HS*/`none`; JWKS fetch discipline = one cache + three cooldowns | Prevents alg-confusion attacks; cooldowns prevent a broken JWKS endpoint from being hammered or a degenerate `{"keys": []}` 200 from evicting a good cache | `app/integrations/supabase_auth.py`: `_ALLOWED_ALGORITHMS = ["ES256","RS256"]`; `_JwksState` singleton + `_FORCED_REFRESH_WINDOW_SECONDS=60`, `_DEGENERATE_FETCH_COOLDOWN_SECONDS=60`, `_FETCH_EXCEPTION_COOLDOWN_SECONDS=5` (TTL 24h) |
| 12 | LLM budget: 20s END-TO-END shared by first attempt + single retry (12s first-attempt cap, 2s retry floor — below the floor the retry is skipped, not attempted); SDK `max_retries=0`; `temperature` OMITTED | The 20s is emergency-prefilter.md's classification budget — per-attempt readings double it; the SDK's hidden retries would too. `temperature` is deprecated on `claude-sonnet-5` (live API 400); determinism is owned by the eval gate, not a sampling knob | `app/integrations/anthropic.py`: `CLASSIFICATION_BUDGET_SECONDS=20.0`, `FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS=12.0`, `MIN_RETRY_BUDGET_SECONDS=2.0`, `new_deadline()`/`attempt_timeout()`, `AsyncAnthropic(..., max_retries=0)`; tests assert the ABSENCE of `temperature` |
| 13 | The emergency line is never gated; nothing in `agent/` reads feature flags | Never-break rules #1/#7: webhooks run on the admin engine so an RLS/GUC failure can never reject an inbound "there is a fire"; flags gate rollouts only, never safety | `app/routers/webhooks/twilio.py` (admin engine + allowlist); `app/agent/emergency.py` docstring ("flag reads are disallowed outright" in `agent/`); root `CLAUDE.md` rules 1 and 7 |
| 14 | On classification failure, never invent a severity | After both attempts fail, `classify_severity` sets `state["classification_failed"]=True`, appends one plain reasoning line, logs — and STOPS. The graph then routes the flag to the `degraded_mode` node (#109, built — see weak point 2) | `app/agent/nodes/classify_severity.py` module docstring "The degraded-mode seam" + the three-things-only failure branch; the conditional edge in `app/agent/graph.py` |

Failed-safe direction to remember when extending any of this: prefer losing an
optimization to losing a message; prefer refusing to boot to serving unscoped.

## Case lifecycle contract

Vocabulary comes from `0002_core_schema.py` CHECKs and
`app/agent/case_lifecycle.py` constants (doc of record:
`docs/02-product/conversation-model.md`).

- **`cases.status`**: `open` → `awaiting_approval` → `awaiting_tenant` →
  `resolved` → `reopened`. **`resolved_reason`**: `landlord` |
  `tenant_confirmed` | `auto_stale`. **`drafts.status`**: `pending`, `stale`,
  `approved`, `sending`, `sent`, `rejected`, `cancelled`.
- **Reopen window = 30 days, INCLUSIVE** (`REOPEN_WINDOW`): a tenant message on
  a case resolved exactly 30 days ago reopens it; older creates a NEW case
  with `related_case_id`. Boundary tested at 29/30/31 days
  (`apps/api/tests/test_case_lifecycle.py`).
- **Auto-stale = 14 days of inactivity** on `last_activity_at`, inclusive
  boundary, resolving with `resolved_reason='auto_stale'`. **`awaiting_approval`
  is EXEMPT** (`AUTO_STALE_ELIGIBLE_STATUSES` = open statuses minus
  `awaiting_approval`): a case with a pending draft is the landlord's backlog,
  never auto-resolved out from under them.
- **`cases.pending_resolved_at` stores the APPLY-AT moment** (proposal time +
  48h, `RESOLUTION_PROPOSAL_WINDOW`), not the proposal instant — "the timer is
  data, not a sleep", same precedent as `drafts.scheduled_send_at`
  (`0008_cases_pending_resolved_at.py`). The tenant-confirmed 48h leg takes
  precedence over the 14-day auto-stale leg.
- **Sweeps are self-guarding UPDATEs**: `sweep_cases()` runs both legs; each
  UPDATE's WHERE re-checks `status` / `pending_resolved_at` /
  `last_activity_at` inside the statement, and the follow-on audit /
  `needs_eyes` writes fire only on `rowcount == 1`. A lost race (a tenant
  texting "actually it's still broken" between decision and write) is a
  deliberate silent no-op — the contradiction wins. This closed a reproduced
  check-then-act (TOCTOU) auto-resolve bug caught pre-merge in the #110 review
  (session-verified 2026-07-05; no repo artifact beyond the shipped
  `app/agent/case_lifecycle.py` fix itself — see `stoop-failure-archaeology`).

## Known-weak points — stated plainly (items 1–2 updated 2026-07-12; 3–8 as of 2026-07-05)

1. **RESOLVED (2026-07-12): the graph IS wired.** PR #185 (squash `69abba4`)
   assembled `app/agent/graph.py` and replaced the `enqueue_classification`
   stub with the real invocation; PR #187 (squash `a61b95e`, 2026-07-10)
   added the shadow-mode `interrupt()` (`await_approval` node), the per-case
   `pg_advisory_xact_lock`, and the checkpoint-keyed completion gate — after
   three pre-merge catches recorded as archaeology A23 (a disproven
   "empirical" replay claim, a TOCTOU resume, a reproduced stuck-draft
   window). The LangGraph interrupt contract A23 pins (whole node re-executes
   on resume; nothing before a raising `interrupt()` commits; plain `ainvoke`
   on a paused thread RESTARTS from START) is load-bearing for #44: the send
   must be a separate node behind a conditional edge, never code after
   `interrupt()`.
2. **Degraded mode (#109) is BUILT (2026-07-12); the emergency executor
   (#108) is still a seam.** PR #188 (squash `161e24c`; migration 0009 adds
   the `tenant_ack`/`degraded_retry` notification types): classification
   failure routes to `app/agent/nodes/degraded_mode.py` — durable holding-ack
   intents (`tenant_ack` rows, `HOLDING_ACK_TEMPLATE`), blind escalation
   (`needs_eyes` with the raw text in the DB payload only, never logs —
   rule #5), and the 1/5/15-minute re-classification retry sweep
   (`app/agent/degraded_mode_sweep.py`) with per-exception Sentry paging and
   a bounded exception counter (`_MAX_CANDIDATE_EXCEPTIONS = 3`) that
   force-escalates instead of looping silently (A24). **Deployment-gating
   fact:** nothing schedules the sweep yet and #108's sender does not exist
   (#108 in flight) — `tenant_ack`/`needs_eyes` rows accumulate undelivered,
   and the no-keyword leg's invariant stays PROVISIONAL until BOTH a
   cron/scheduler and the sender are energized (the sweep module's
   "DEPLOYMENT-GATING FACT" docstring states this plainly — repeat it in any
   deployment/cutover plan). `app/agent/emergency.py::fire_emergency_protocol`
   still logs and returns — no voice call, no safety SMS, no escalation
   chain.
3. **The web dashboard is a mock-data shell.** `apps/web/src/lib/mock-app.ts`
   feeds the routes; there is no API client, no Supabase JS dependency in
   `apps/web/package.json`, and no Supabase session in the browser. `/v1/queue`
   does not exist server-side (routers today: `health`, `me`,
   `webhooks/twilio`, `auth_test`, `debug`). There is no frontend CI
   (`.github/workflows/ci.yml` has only the backend job), and
   `apps/web/wrangler.jsonc` still has the D1 placeholder
   `"database_id": "REPLACE_WITH_D1_ID"`.
4. **Documented contract gaps between `docs/03-engineering/api-contracts.md`
   and the dashboard design**: the `/v1/queue` `counts` object is
   `{total, emergency, urgent, routine}` — no `awaiting_tenant`; queue items
   carry `reasoning: string[]` while the in-flight dashboard rebuild renders a
   single prose `why` string (session-verified 2026-07-05; no repo artifact);
   queue items have no `media` field even though case-timeline messages do.
5. **The intent prompt is inline and unversioned**:
   `_INTENT_PROMPT_VERSION = "inline-v0"` in
   `app/agent/nodes/classify_intent.py`, deliberately distinct from the
   versioned prompts package (`app/agent/prompts/` — v1 frozen history, v2
   on `main` since PR #177 (2026-07-06), each pinning its own
   `PROMPT_VERSION`). Moving it into a frozen `prompts/v{n}.py` file is
   unfinished business — until then, intent-prompt changes lack the
   frozen-version discipline severity prompts have.
6. **`apps/api/CLAUDE.md`'s "Layout (target)" has drifted**: `app/models/`
   does not exist (SQL is raw `text()` + migrations), `app/agent/graph.py`
   does not exist, and most listed routers (properties, tenants, vendors,
   queue, cases, drafts, notifications, billing, webhooks/stripe) are
   unbuilt. Treat that section as the target, not the map.
7. **RLS role separation is not active in any deployed environment.** The
   live Supabase project (ref `kytqtdqmzwyhiwkafcbh`, ca-central-1, migrations
   at head 0008) still has `app_role` as `NOLOGIN` and `APP_DATABASE_URL` has
   never been set anywhere — the one-time operator flip in
   `app/db/session.py`'s module docstring has not been done (session-verified
   2026-07-05; no repo artifact). Until then every request session runs on the
   admin engine under the documented fallback WARNING. Do the flip BEFORE any
   real tenant data exists; see `stoop-run-and-operate`.
8. **Four safety-improvement rounds landed via PR #177** (squash
   `3ddd15e`, merged to main 2026-07-06): the `_UNSAFE_HEAT_SOURCE_RE` hard guard in
   `draft_response.py` (rejects drafts suggesting an oven/stovetop/open flame
   for warmth — a real CO hazard an eval judge caught); the prefilter
   verb-tense sweep ("smelled like gas" etc.); the refusal-ack instruction
   rewrite (`33441d2` — the ack no longer duplicates the appended hand-off);
   and the live prompt-version switch to **prompts v2** (`11564c8` +
   `31bd498` — plain-language refusal templates;
   `classify_severity`/`draft_response`/`evals.runner` all import
   `prompts.v2` and drafts/audit stamp `prompt_version="v2"`). The branch's
   eval gate is GREEN — gate 9: 20/20, `release_blocked=False` (2026-07-06),
   baseline committed as `apps/api/evals/results/v1-baseline.json`
   (`7fe8609`) — but `git grep _UNSAFE_HEAT_SOURCE_RE main` returns nothing:
   none of it protects production until merged. Merging the eval-harness PR
   is load-bearing for draft safety — see `stoop-core-loop-campaign`.

## Provenance and maintenance

Re-verify before relying on a drift-prone claim (run from the repo root
unless a `cd` is shown; all read-only):

| Claim | One-line re-verification |
|---|---|
| Graph wired, interrupt + case lock present (#185/#187) | `grep -n "pg_advisory_xact_lock\|add_conditional_edges" apps/api/app/agent/graph.py \| head -3` |
| #34/#43/#109 closed; #44/#45/#108 remaining | `for n in 34 43 109 44 45 108; do gh issue view $n --repo LaithAlz/stoop-backend --json number,state -q '"\(.number) \(.state)"'; done` |
| Degraded mode built; sweep still uncalled outside tests | `grep -n "HOLDING_ACK_TEMPLATE" apps/api/app/agent/nodes/degraded_mode.py; grep -rn "sweep_degraded_mode_retries" apps/api/app --include='*.py' \| grep -v degraded_mode_sweep` |
| No Twilio send call site in app code | `grep -rn "twilio.rest\|from twilio\|import twilio" apps/api/app/ apps/api/pyproject.toml` |
| Append-only REVOKEs in 0005 | `grep -n "REVOKE UPDATE, DELETE" apps/api/migrations/versions/0005_app_role_and_rls.py` |
| One-pending-draft index | `grep -n uq_drafts_one_pending apps/api/migrations/versions/0002_core_schema.py` |
| Tier-0 clamp present | `grep -n classify_severity_tier0_clamp apps/api/app/agent/nodes/classify_severity.py` |
| Admin-session allowlist test | `grep -n "_ADMIN_SESSION_ALLOWLIST" apps/api/tests/test_migrations_0005.py` |
| GUC set only in require_landlord | `grep -rn "app.current_landlord_id" apps/api/app/ \| grep -v migrations` |
| Boot gates (prod env vars) | `grep -n "_require_.*_in_production" apps/api/app/config.py` |
| Checkpointer schema + knobs | `grep -n "LANGGRAPH_SCHEMA\|prepare_threshold\|autocommit" apps/api/app/agent/checkpointer.py` |
| Webhook dedupe index | `grep -n uq_notifications_message_dedupe apps/api/migrations/versions/0006_notifications_message_dedupe_index.py` |
| JWKS three cooldowns | `grep -n "_WINDOW_SECONDS\|_COOLDOWN_SECONDS" apps/api/app/integrations/supabase_auth.py` |
| LLM budget constants / max_retries | `grep -n "BUDGET_SECONDS\|max_retries\|TIMEOUT_CAP\|MIN_RETRY" apps/api/app/integrations/anthropic.py` |
| Lifecycle windows (30d/14d/48h) | `grep -n "REOPEN_WINDOW\|AUTO_STALE_INACTIVITY\|RESOLUTION_PROPOSAL_WINDOW" apps/api/app/agent/case_lifecycle.py` |
| messages columns still deprecated (v1.6) | `grep -n "DEPRECATED v1.6" docs/03-engineering/schema-v1.md` |
| Queue counts still lack awaiting_tenant | `grep -n '"counts"' docs/03-engineering/api-contracts.md` |
| Intent prompt still inline-v0 | `grep -n inline-v0 apps/api/app/agent/nodes/classify_intent.py` |
| Web still a mock shell / no supabase dep | `grep -rln mock apps/web/src/routes \| head -3 ; grep -c supabase apps/web/package.json` |
| Frontend CI still absent | `grep -c "apps/web" .github/workflows/ci.yml` |
| D1 placeholder still unset | `grep -n database_id apps/web/wrangler.jsonc` |
| Heat-source guard merged to main yet? | `git grep -c _UNSAFE_HEAT_SOURCE_RE main -- apps/api/app/agent/nodes/draft_response.py \|\| echo NOT_ON_MAIN` |
| Full invariant suite (needs local DB; never the live pooler) | `cd apps/api && uv run pytest -m "not eval" -q` — see `stoop-validation-and-qa` before running anything heavier |
