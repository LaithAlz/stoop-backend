---
name: stoop-diagnostics-and-tooling
description: Measure the Stoop backend instead of eyeballing it. Load this skill when you need to check whether the eval gate passed or why release_blocked is true, summarize or interpret apps/api/evals/results/last-run.json, diagnose an eval scenario failure or a judge that fails drafts its own prose calls good (judge verdict inversion / CHECK-INVERSION), test whether the Tier-0 emergency prefilter catches a specific phrase, see what the agent decided via audit_log (severity, rules_fired, tokens_in/out, cost_cents), verify RLS is actually enabled and messages/audit_log are append-only, confirm the admin and request DB engines are role-separated, understand the JWKS cache cooldowns behind flaky 401s, figure out how much a run or classification cost, grep structlog events, use the /_debug routes, or sanity-check the toolchain (.env var names present, docker postgres healthy) without ever printing a secret. Ships three tested scripts — scripts/env-check.sh, scripts/eval-summary.py, scripts/db-probes.sql.
---

# Stoop diagnostics and tooling — measure, don't eyeball

This skill owns **measurement and interpretation**: the commands that answer
"is it actually working?" with numbers and structured output, and the guides
for reading what comes back. Repo root: the monorepo top (contains
`apps/api`, `docs/`, `docker-compose.yml`). All commands are written to run
from repo root unless prefixed with `cd apps/api`.

Jargon used once, defined once:
- **Tier-0 prefilter** — deterministic keyword emergency filter
  (`apps/api/app/agent/prefilter.py`), pure functions, runs in the Twilio
  webhook before any LLM call.
- **Eval gate** — the 20-scenario corpus run by `apps/api/evals/runner.py`
  (11 LLM scenarios with 3 classification samples + 1 draft + 1 LLM-judge
  call each, plus 9 Tier-0-only negative scenarios n1–n9).
- **`last-run.json`** — `apps/api/evals/results/last-run.json`, the full
  per-scenario report the runner ALWAYS writes (every invocation, real or
  dry-run, even on crash paths). Gitignored, overwritten every run. Its
  sibling `v1-baseline.json` — the COMMITTED gate-9 green baseline (20/20,
  2026-07-06, commit `7fe8609`, root-`.gitignore` exception) — is the stable
  comparison point.
- **Hard fail** — a failed `emergency`/`refusal`-category scenario (or ANY
  Tier-0 prefilter assertion failure, regardless of category). Blocks release.
- **Soft fail** — a failed `urgent`/`routine` scenario. Blocks prompt
  promotion, not development.
- **Infra error / inconclusive** — the harness could not obtain a model
  output at all (`ScenarioInfraError`); never counted as a semantic miss,
  but still blocks release ("we don't know" is not shippable).

## When NOT to use this skill

| You actually want | Go to |
|---|---|
| Fix a failure you just diagnosed / ship a change through gates and reviewers | `stoop-change-control` |
| Symptom-to-triage flows, shell traps (piped exit codes, placeholder `DATABASE_URL` connection storms) | `stoop-debugging-playbook` |
| The incident history behind these tools (judge inversion, 429 storms, flaky 401s) | `stoop-failure-archaeology` |
| Recreate the dev environment from scratch | `stoop-build-and-env` |
| Run/migrate/operate commands, live-DB discipline, operator flips (`app_role` LOGIN) | `stoop-run-and-operate` |
| Config axes, defaults, production boot gates | `stoop-config-and-flags` |
| What counts as merge evidence; test/eval discipline | `stoop-validation-and-qa` |
| Two-engine design rationale and invariants | `stoop-architecture-contract` |
| Deeper prove-it recipes with worked examples | `stoop-proof-and-analysis-toolkit` |

## Shipped scripts (all tested against this repo, 2026-07-06)

| Script | What it does | Run from repo root |
|---|---|---|
| `scripts/env-check.sh` | Toolchain + env sanity: uv present, docker daemon up (15s timeout, never hangs), compose postgres healthy, `.env` exists, required var NAMES present. **Never prints a value** — `grep -c` on names only. Exit = failure count. | `.claude/skills/stoop-diagnostics-and-tooling/scripts/env-check.sh` |
| `scripts/eval-summary.py` | Compact table + gate verdict from `last-run.json`, plus CHECK-INVERSION / JUDGE-KEY-MISMATCH warnings (§2.3). Read-only, free. Exit 0 = gate clear, 1 = release_blocked, 2 = file missing. | `python3 .claude/skills/stoop-diagnostics-and-tooling/scripts/eval-summary.py` |
| `scripts/db-probes.sql` | READ-ONLY probe pack: alembic head, RLS status + policies, role flags, append-only grants, row counts, latest audit actions. **Local docker or explicitly-authorized live reads only.** | `docker compose exec -T postgres psql -U stoop -d stoop < .claude/skills/stoop-diagnostics-and-tooling/scripts/db-probes.sql` |

Never `source` or `cat` `apps/api/.env` — a sourced `.env` once echoed a live
Twilio auth token into a terminal log and forced a credential rotation
(session-verified 2026-07-05; no repo artifact). `env-check.sh` is the safe
way to ask "is my env complete?".

## 1. The measurement inventory

| Question | Tool | Command | How to read the answer |
|---|---|---|---|
| Is the eval harness machinery intact? | dry-run runner (free, zero API calls) | `cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner` | All 20 scenarios PASS, exit 0 — the stub synthesizes textbook answers from each scenario's `expect` block, so ANY failure here is a loader/scoring/reporting bug, never model quality. **Caution: this overwrites `last-run.json` even in dry-run** — copy the file first if a paid gate's output is still needed. Zero-side-effect pre-checks: `uv run python -c "import evals.runner"` and `uv run pytest tests/test_evals.py --collect-only -q` (115 tests as of 2026-07-06). |
| Did the gate pass? | `eval-summary.py` | `python3 .claude/skills/stoop-diagnostics-and-tooling/scripts/eval-summary.py` | `release_blocked = any hard failure OR any errored (inconclusive) scenario` (`evals/scoring.py::GateVerdict`). `python -m evals.runner`'s exit code (1 if blocked) is the merge decision for prompt/rubric changes — NOT raw pytest exit codes, which also redden on soft fails. |
| Is Tier-0 catching phrase X? | prefilter one-liner (pure function, no I/O) | `cd apps/api && uv run python -c "from app.agent.prefilter import check; print(check('the kitchen has smelled like gas since I got home'))"` | Prints `PrefilterResult`: `hard_hit=True` fires the emergency protocol pre-LLM; `categories` names the triggers (e.g. `['gas_co']`); `guards` lists suppressions that matched (e.g. `['smoke_detector_battery']`). Regression suite: `cd apps/api && uv run pytest tests/test_prefilter.py -m unit -q` (281 tests, <1s, as of 2026-07-06). Changes are additive-only with a regression test class each — see `stoop-change-control`. |
| What did the agent decide (and what did it cost)? | `audit_log` queries | See query patterns below this table | `action='classified'` payload: `severity, rules_fired, modifier, refusal_flags, model, tokens_in, tokens_out, cost_cents, prompt_version`. `action='drafted'` payload: `draft_id, refusal_templates_used, guard_failed, model, tokens_in, tokens_out, cost_cents`. Full action vocabulary = the CHECK constraint in `migrations/versions/0002_core_schema.py`. |
| Is RLS actually on? Are the append-only REVOKEs real? | `db-probes.sql` | See scripts table above | Expect: 13 public tables `rls_enabled=t, rls_forced=f` (ENABLE-not-FORCE is deliberate — FORCE broke first-login provisioning); 13 `*_isolation` policies `TO app_role` with USING+WITH CHECK; §4b shows `app_role` UPDATE/DELETE `= f` on `messages`/`audit_log`/`message_status_events` (never-break rule #2). Local docker runs as bootstrap superuser and is blind to privilege bugs — live proof needs a live dry-run (`stoop-run-and-operate`). |
| Are the two DB engines actually role-separated? | startup self-check | Runs automatically at FastAPI startup; manual: `cd apps/api && uv run python -c "import asyncio; from app.db.session import verify_request_engine_role_separation as v; asyncio.run(v())"` | No-op when `APP_DATABASE_URL` unset (you'll have seen the one-time `rls_not_enforced_by_role_separation` WARNING at import). When set: raises `RoleSeparationVerificationError` and refuses startup if the request role has `rolbypassrls` (the `postgres`/`service_role` mistake) OR equals the admin role (copy-paste error). Failure log events in §3.1. Both sides compare SERVER-reported `current_user` — under Supavisor the client-side username (`role.project-ref`) never matches the server-side bare role, so URL-parsing checks are wrong by design. |
| What is the JWKS cache doing? | code constants + tests (deliberately not logged) | `grep -n '_SECONDS' apps/api/app/integrations/supabase_auth.py` | Three independent cooldowns (as of 2026-07-06): `_FORCED_REFRESH_WINDOW_SECONDS=60` (unknown-`kid` forced refresh, DoS guard), `_DEGENERATE_FETCH_COOLDOWN_SECONDS=60` (empty `{"keys": []}` 200s, #147), `_FETCH_EXCEPTION_COOLDOWN_SECONDS=5` (connect errors/5xx/timeouts — short so a blip can't block cold-cache warmup 60s, #158). Cache TTL 24h, per-process singleton. The module's ONLY log event is `auth_verified` (auth_user_id only — rule #5); cache misbehavior surfaces as 401s, not log lines. Test isolation: conftest autouse fixtures call `_jwks_state.reset_for_tests()`; never nest respx contexts (the flaky-401 family — `stoop-failure-archaeology`). |
| How much did a run cost? | `last-run.json` / `eval-summary.py` / `audit_log` | Summary script prints `total run cost`; per-scenario `cost_cents`, per-sample `cost_cents` in the JSON. Production: sum `(payload->>'cost_cents')::numeric` over `classified`/`drafted` audit rows. | Gate 7 (2026-07-06T01:07Z record, prompt v1): 81.27¢; gate 8 (06:26Z record, prompt v2): 73.65¢; gate 9 (06:43Z record, prompt v2 — the 20/20 GREEN run, `release_blocked=False`): 84.02¢. The file is overwritten every run — run `eval-summary.py` rather than trusting any pinned figure. Per-run cost/duration range: `stoop-change-control` rule 9. Pricing model: $3.00/$15.00 per MTok in/out, hardcoded conservative placeholder (`apps/api/app/integrations/anthropic.py::estimate_cost_cents`). |
| Is my toolchain/env sane? | `env-check.sh` | See scripts table above | Required var names come from the no-default fields in `apps/api/app/config.py`: `DATABASE_URL, SUPABASE_URL, SUPABASE_JWKS_URL, SUPABASE_JWT_ISSUER, SUPABASE_SERVICE_ROLE_KEY, TWILIO_AUTH_TOKEN, ANTHROPIC_API_KEY`. `APP_DATABASE_URL`/`PUBLIC_BASE_URL` are optional in dev but production refuses to boot without them (`config.py` boot gates — `stoop-config-and-flags`). |

**Paid-run discipline:** `uv run python -m evals.runner` WITHOUT `EVAL_DRY_RUN=1`
(and `uv run pytest -m eval`) hits the real Anthropic API and costs real money —
it is the orchestrator's call with the founder's go-ahead, never something an
agent fires unilaterally (`evals/runner.py` module docstring banner). Bare
`uv run pytest` excludes the paid `eval` marker via the `addopts` guard added in PR #177; when in doubt still default to
`uv run pytest -m "not eval"`.

Audit query patterns (validated against the local docker DB, 2026-07-06):

```sql
-- What did the agent decide on a case?
SELECT created_at, actor, action,
       payload->>'severity'   AS severity,
       payload->'rules_fired' AS rules_fired,
       payload->>'cost_cents' AS cost_cents
FROM audit_log
WHERE case_id = '<uuid>' AND action IN ('classified','drafted')
ORDER BY created_at;

-- LLM spend, last 24h
SELECT action, count(*) AS n, sum((payload->>'cost_cents')::numeric) AS cents
FROM audit_log
WHERE action IN ('classified','drafted') AND created_at > now() - interval '1 day'
GROUP BY action;
```

## 2. Interpretation guides

### 2.1 `last-run.json`, field by field

Top level: `generated_at`, `prompt_version` (currently `v2` — the live
prompt version on `main` since PR #177 (2026-07-06); pre-2026-07-06
reports say `v1`; the value tracks whichever frozen `prompts/v{n}` module
the runner imported, so a `v2` header is correct, not drift),
`rubric_version` (`1.0`), `scenarios[]`, `summary`.

`summary`: `total`, `passed`, `failed`, `hard_failed_scenario_ids`,
`soft_failed_scenario_ids`, `errored_scenario_ids`, `release_blocked`.

Per scenario:

| Field | Meaning / gotcha |
|---|---|
| `scenario_id`, `category` | Category ∈ `emergency, refusal, urgent, routine`. `hard_fail_class` = category is emergency/refusal. |
| `prefilter.{hard_hit, categories, guards, expected, ok}` | `ok=None` means the scenario asserts nothing about Tier-0. `ok=False` is hard-fail-worthy REGARDLESS of category (a Tier-0 regression is safety-critical in either direction). |
| `classification_samples[]` | 3 per LLM scenario, EMPTY for the 9 `tier0_only` negatives (n1–n9 — their `draft` is `null` too; that's by design, not truncation). Each: `severity, rules_fired, modifier, refusal_flags, derived_actions, cost_cents, latency_s, retries, ok, failures`. `derived_actions` is computed FROM severity (no model "actions" field exists) — traceability only. Flaky = fail: all 3 samples must pass. |
| `classification_samples[].retries` | Transport-level 429/529 backoff retries only — NEVER a 4th sample, never a semantic retry. Diagnostic: "passed but needed 3 retries" = rate-limit pressure, not model trouble. |
| `latency_s` (everywhere) | Model latency only — harness pacing/backoff sleep is subtracted out. |
| `draft.{draft_body, hard_guard_violations, guard_failed, length_over_budget, judge_reasoning, failures, ...}` | `guard_failed=true` = hard guards violated twice → the real node would send the generic safe fallback → always a scenario failure. `length_over_budget=true` explains a long body (regeneration attempted, truncation forbidden). `judge_reasoning` is the judge's prose — the cross-check anchor in §2.3. |
| `passed` / `is_hard_failure` / `errored` / `infra_error` | Mutually exclusive verdict trio: PASS, HARD-FAIL/SOFT-FAIL, or INFRA (errored, `infra_error` holds the message). |
| `cost_cents`, `top_level_failures` | Scenario cost = 3 samples + draft + judge. |

### 2.2 Infra failure vs semantic failure

- A **semantic** failure = the model produced an answer and it was wrong
  (severity mismatch, missing refusal flag, judge boolean false). It counts
  toward hard/soft fail.
- An **infra** failure (`ScenarioInfraError`: backoff budget exhausted, any
  other `AnthropicCallError`, or Pydantic validation of a malformed
  response) = no gradable output existed. The scenario is INCONCLUSIVE —
  re-run it; it is never recorded as a rubric miss, but it still sets
  `release_blocked`.
- Harness pacing (as of 2026-07-06, `evals/runner.py`): token budget
  `EVAL_TOKEN_BUDGET_PER_MIN` (default 25000, sliding 60s window of actual
  `tokens_in`; per-call estimates classify 4500 / draft 2000 / judge 1500),
  1s pace floor, exponential backoff base 2s capped 70s, max 6 retries,
  retryable ONLY when `exc.__cause__` is `RateLimitError` (429) or
  `OverloadedError` (529). Runner docstring estimates 12–18 min per full
  real run; observed founding-session durations live with the cost figures
  in `stoop-change-control` rule 9.

### 2.3 Judge-inversion cross-check (runbook)

Background: in gate 5, scenarios hard-failed on drafts whose own
`judge_reasoning` said "Fully conformant" — the judge returned correct
verdicts under dict keys that didn't exactly match the checklist strings,
and the old exact-string lookup silently defaulted every item to False.
Full writeup: `apps/api/evals/judge.py` module docstring, "BLOCKING bug
found in gate 5 triage".

Whenever a scenario fails on judge booleans:

1. Run `eval-summary.py`. A `CHECK-INVERSION` warning (judge failures + no
   negative keyword in the prose) or `JUDGE-KEY-MISMATCH` warning (the
   scorer's distinct `NO MATCHING KEY` failure text) means suspect
   eval-infra, not product.
2. Heuristic limits: prose with an incidental negative word suppresses the
   warning — so ALWAYS also read `draft.judge_reasoning` yourself and
   compare it against each `judge: ...` failure string.
3. Prose and booleans DISAGREE (prose says present/absent correctly,
   booleans failed) → eval-infra bug. Fix surface, in order checked:
   tolerant lookup `_lookup_checklist_item`/`_normalize_checklist_key`
   (`evals/scoring.py`), the numbered UNQUOTED checklist + verbatim-key
   instruction in the judge prompt (`evals/judge.py`), wrapper-unwrap
   validators (`_unwrap_single_key_wrapper` in `app/agent/schemas.py`,
   mirrored on `JudgeVerdict`).
4. `NO MATCHING KEY` in a failure string is by construction a key-shape
   mismatch (the fix made it a distinct, loud outcome — never silently
   False). Treat as infra.
5. Prose and booleans AGREE the draft is bad → genuine product/prompt
   issue → `stoop-change-control` (prompt changes = new version file +
   full eval run).
6. Governance: `evals/judge.py` is EVAL INFRASTRUCTURE — editable in place,
   NOT subject to the frozen-prompt discipline (its docstring says so
   explicitly). Product prompts (`app/agent/prompts/v{n}.py` — v1 AND v2,
   both frozen) are never edited in place; a behavior change means v3
   (`stoop-change-control` §6).

## 3. Log reading

Structlog emits JSON lines to stdout; `request_id` is bound by
`RequestIDMiddleware` (`app/middleware/request_id.py`), with
`landlord_id`/`case_id`/`message_id` bound where known. Grep by event name,
e.g. `grep classify_severity_tier0_clamp`.

### 3.1 Event names worth grepping (verified in code, 2026-07-06)

| Event | Level | Means |
|---|---|---|
| `classify_severity_tier0_clamp` | warning | The LLM tried to classify below EMERGENCY on a hard Tier-0 fire; the clamp re-escalated (fields: `message_id`, `llm_severity`, `categories`). Frequent hits = prompt/rubric drift worth an eval scenario. |
| `classify_severity_attempt_failed` / `classify_severity_failed_after_retry` / `classify_severity_retry_skipped_budget_exhausted` | error | Classification call failures inside the 20s budget (`CLASSIFICATION_BUDGET_SECONDS`); `failed_after_retry` → `classification_failed=True` → degraded-mode path. |
| `draft_response_guard_violation` / `draft_response_guard_failed_after_retry` | warning/error | Hard guard (dollar_compensation, access_code, legal_position, unsafe_heat_source) tripped on the model's ack; `failed_after_retry` = generic safe fallback was used. |
| `draft_response_length_violation` / `draft_response_length_over_budget_kept` | warning | Over the SMS length budget; `kept` = still long after one regeneration (truncation forbidden). |
| `rls_not_enforced_by_role_separation` | warning | Once per process at import: `APP_DATABASE_URL` unset, request sessions on the admin engine. Expected in dev/CI; in production it cannot appear (boot gate). |
| `rls_role_separation_verification_failed` | error | Self-check refusing startup: `request_bypassrls=True` or `request_user == admin_user` (role names only in the line). |
| `rls_role_separation_self_check_connect_failed` | error | Self-check could not connect (only `exc_type` — check credentials/connectivity). |
| `auth_verified` | info | Successful JWT verification; `auth_user_id` only. The ONLY auth-path event. |
| `twilio_webhook_signature_rejected` / `twilio_sms_unknown_to_number` / `twilio_sms_post_persist_stage_failed` / `twilio_sms_tenant_hard_fire` | warning / error / error / error | Webhook path: bad signature; inbound to a number no landlord owns; post-commit processing failed (5xx returned so Twilio retries — the message row is already safe); Tier-0 fired on a tenant message (deliberately error + Sentry capture — uuids and category names only). |
| `case_lifecycle_sweep_complete` / `case_lifecycle_sweep_guard_miss` | info | Stale-case sweeper; `guard_miss` = a self-guarding UPDATE matched 0 rows (a tenant contradicted resolution mid-sweep — working as designed). |
| `identify_property_matched` / `identify_property_unknown_sender` | info | Sender→property resolution. |
| `checkpointer_setup_complete` / `checkpointer_setup_failed` | info/error | LangGraph checkpointer schema init. |
| `weather_lookup_unavailable` | warning | Weather enrichment degraded (classification proceeds without it). |
| `debug_log_endpoint_called` | info | The `/_debug/log` smoke event (§4). |

### 3.2 The no-PII discipline — what you will NEVER find (by design)

Never-break rule #5: no JWTs/Authorization headers (or substrings), no
tenant phone numbers, no message bodies, no connection strings/passwords in
any log line, exception message, or error envelope. What you get instead:
`auth_user_id`, row uuids (`message_id`/`case_id`/`landlord_id`),
`exc_type` names, bare role names. Consequences for diagnosis: you cannot
reconstruct message content from logs — join uuids against the database
(§1 audit queries) instead. **If you ever see a body, phone number, or
token in a log line, that is itself a release-blocking bug — report it.**

## 4. `/_debug` routes (dev-only)

Registered by `create_app()` ONLY when `not settings.is_production`
(`app/main.py`); they do not exist in production.

| Route | Purpose | Expected result |
|---|---|---|
| `GET /_debug/log` | Structured-logging smoke test | 200 `{"status": "logged"}` + a `debug_log_endpoint_called` JSON line on stdout carrying `request_id` and `check=structlog_ok` — proves JSON logging and context propagation are wired. |
| `GET /_debug/error` | Sentry capture smoke test | Deliberate `RuntimeError` → 500; captured by Sentry when a DSN is configured, plain 500 otherwise. |

Server for local testing: `cd apps/api && uv run uvicorn app.main:app --reload`
(needs `.env`; run `env-check.sh` first).

## Provenance and maintenance

Volatile claims and their one-line re-verification commands (run from repo
root unless noted; all values date-stamped 2026-07-06):

| Claim | Re-verify with |
|---|---|
| Required env var names (7 no-default fields) | `cd apps/api && uv run python -c "from app.config import Settings; print(sorted(n.upper() for n,f in Settings.model_fields.items() if f.is_required()))"` |
| JWKS cooldowns 60/60/5s, TTL 24h | `grep -n '_SECONDS' apps/api/app/integrations/supabase_auth.py` |
| Gate = 20 scenarios (11 LLM + 9 tier0-only), current verdict | `python3 .claude/skills/stoop-diagnostics-and-tooling/scripts/eval-summary.py` |
| Runner pacing/backoff constants (25000 budget, 70s cap, 6 retries) | `grep -nE 'EVAL_TOKEN_BUDGET_PER_MIN|RATE_LIMIT_MAX_RETRIES|BACKOFF_CAP' apps/api/evals/runner.py` |
| `release_blocked` semantics (hard OR errored) | `grep -n -A3 'def release_blocked' apps/api/evals/scoring.py` |
| structlog event names in §3.1 | see the fenced command below this table (pipes don't survive a markdown table cell) |
| audit_log action vocabulary | `grep -n -A6 'action.*CHECK' apps/api/migrations/versions/0002_core_schema.py` |
| `classified`/`drafted` payload keys | `grep -n -B2 -A12 'payload = {' apps/api/app/agent/nodes/classify_severity.py` and `grep -n -A12 '"payload": json.dumps' apps/api/app/agent/nodes/draft_response.py` |
| Prefilter import + result shape | `cd apps/api && uv run python -c "from app.agent.prefilter import check; print(check('smoke everywhere'))"` |
| `/_debug` registration condition | `grep -n -B1 -A3 'is_production' apps/api/app/main.py` |
| Append-only grants + RLS state (local) | run `scripts/db-probes.sql` (§4b: UPDATE/DELETE must be `f`) |
| Anthropic pricing constants ($3/$15 per MTok) | `grep -n 'PRICE_PER_MTOK' apps/api/app/integrations/anthropic.py` |
| Prefilter/prompt/rubric version pins | `grep -n 'PREFILTER_VERSION' apps/api/app/agent/prefilter.py` and the `prompt_version`/`rubric_version` header of `last-run.json` |
| Migration head (0008 as of 2026-07-06) | `ls apps/api/migrations/versions/` vs probe §0 |
| Test counts (115 eval-harness, 281 prefilter) | `cd apps/api && uv run pytest tests/test_evals.py tests/test_prefilter.py --collect-only -q` (count on the last line) |

Event-name extraction (the §3.1 verification command — kept out of the
table because pipes don't copy-paste from markdown table cells):

```bash
cd apps/api && grep -rn 'log\.\(info\|warning\|error\)(' app/ --include='*.py' -A1 \
  | grep -o '"[a-z0-9_]\{6,\}"' | sort -u
```

Drift rule: when any command above disagrees with this file, the repo wins —
update this SKILL.md (and the script expectations it documents) in the same
change, per the amendment discipline in `stoop-docs-and-writing`.
