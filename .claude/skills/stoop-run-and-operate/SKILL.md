---
name: stoop-run-and-operate
description: Run, migrate, and operate the Stoop API. Load this when you need to start the server (uv run uvicorn app.main:app), understand what happens at startup (config validation, role-separation self-check, checkpointer setup, "Application startup failed"), run or write Alembic migrations (uv run alembic upgrade head / downgrade -1, round-trip rule, where DATABASE_URL comes from), dry-run a role/grant/RLS migration against the LIVE Supabase database, perform or document operator flips (app_role LOGIN password, APP_DATABASE_URL, PUBLIC_BASE_URL), operate or locally test the Twilio webhooks (/webhooks/twilio/sms 5xx-by-design, signature verification, twilio_sid dedupe), launch a paid eval run (python -m evals.runner, EVAL_DRY_RUN, last-run.json, --snapshot), or reason about deploy state (Dockerfile, missing fly.toml, wrangler D1 placeholder, what lands in logs/Sentry/audit_log). Operating only — not environment setup, not debugging, not change process.
---

# Stoop — run, migrate, operate

Repo root: `/Users/laith/Businesses/LandlordAI`. Backend: `apps/api` (Python 3.12 / FastAPI / uv). All commands below are run from `apps/api` unless stated otherwise. "Founder" = the human project owner; "orchestrator" = the lead agent coordinating a work thread (never an implementer subagent).

## When NOT to use this skill

| You are trying to… | Load instead |
|---|---|
| Set up a fresh machine, install uv/Docker/Bun, create `.env`, fix "204 connection errors" | `stoop-build-and-env` (also the home of the safe `.env` export pattern and the never-`source .env` rule) |
| Understand every config variable, defaults, and boot gates in detail | `stoop-config-and-flags` |
| Triage a failure symptom you don't recognize | `stoop-debugging-playbook` |
| Read the history of an incident ("why is it built this way?") | `stoop-failure-archaeology` |
| Ship a change (branch/PR/review gates) | `stoop-change-control` |
| Interpret eval scores, decide what counts as evidence | `stoop-validation-and-qa` |
| Supabase platform facts, rubric doctrine, SMS/LLM safety theory | `stoop-domain-reference` |

## 1. Running the API: command anatomy and startup lifecycle

Dev run:

```bash
cd apps/api && uv run uvicorn app.main:app --reload
```

`app/main.py` builds the app at import time (`app = create_app()`). What registers, in order (`create_app` docstring is authoritative):

1. `configure_logging()` — structlog, single-line JSON to stdout (`app/observability.py`).
2. `init_sentry()` — no-op unless `SENTRY_DSN` is set. When set, `send_default_pii=False` and `include_local_variables=False` are mandatory (JWT-leak defense) — never flip them.
3. `init_langsmith_tracing()` — no-op unless `LANGSMITH_API_KEY` is set.
4. `RequestIDMiddleware`, then `AuthError` (401) and `AppError` exception handlers — all errors use the envelope `{"error": {"code", "message", "request_id"}}`.
5. Routers: `health` (`/healthz`, `/readyz`), `me` (`/v1/me`), Twilio webhooks (always registered — Twilio must reach them in every environment), `auth_test` (`/v1/auth-test`, always), and `debug` (`/_debug/log`, `/_debug/error`) **only when `settings.is_production` is false**. If `/_debug/*` 404s, you are in `ENVIRONMENT=production` — that is correct behavior, not a bug.

### Startup lifecycle — two fail-closed phases

**Phase 1 — import time.** `app/config.py` constructs the `Settings` singleton at module import. Required with no default: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER`, `SUPABASE_SERVICE_ROLE_KEY`, `TWILIO_AUTH_TOKEN`, `ANTHROPIC_API_KEY`. Failure looks like a `pydantic.ValidationError` traceback during import — uvicorn never binds a port. Two production-only gates raise `ValueError` inside that same validation: `ENVIRONMENT=production` refuses to boot without `APP_DATABASE_URL` (RLS role separation, #22) and without `PUBLIC_BASE_URL` (Twilio signature verification must not trust proxy headers, #40/#152). Whitespace-only values are normalized to unset — a blank Fly secret cannot sneak past the gate.

**Phase 2 — ASGI lifespan** (`_lifespan` in `app/main.py`), runs before any request is served:

1. `verify_request_engine_role_separation()` (`app/db/session.py`) — no-op when `APP_DATABASE_URL` is unset. When set, it queries **both** engines for their SERVER-side `current_user` and raises `RoleSeparationVerificationError` if the request role has `rolbypassrls = TRUE` (you pointed `APP_DATABASE_URL` at `postgres`/`service_role` by mistake) or if both engines report the same role (copy-paste of `DATABASE_URL`). Server-side comparison is deliberate: under Supavisor (Supabase's connection pooler) the client-side username is `role.project-ref` while the server reports the bare role, so URL parsing can never be trusted.
2. `setup_checkpointer()` (`app/agent/checkpointer.py`) — idempotently creates/migrates the LangGraph checkpoint tables inside the dedicated `langgraph` schema (migration 0007), on its own psycopg3 pool built from `DATABASE_URL` (never `APP_DATABASE_URL`). Pool knobs are load-bearing: `prepare_threshold: None` (NOT `0` — `0` means prepare-everything), `autocommit=True` (the library runs `CREATE INDEX CONCURRENTLY`), and `search_path` pinned twice (libpq `options` startup parameter + a `configure` callback).

Both phases raise and abort startup — uvicorn prints `Application startup failed`. This is by design: serving traffic with broken role separation or a broken checkpoint store is worse than not starting. Shutdown symmetrically calls `close_checkpointer()`.

When `APP_DATABASE_URL` is unset outside production, request sessions fall back to the admin engine and a **one-time boot WARNING** notes RLS is not yet enforced by role separation — expected in local dev and CI.

## 2. Migrations: anatomy and the round-trip rule

```bash
cd apps/api && uv run alembic upgrade head      # apply
cd apps/api && uv run alembic downgrade -1      # revert one revision
cd apps/api && uv run alembic current           # show applied revision
```

Facts that shape how you write and run them:

- **URL resolution** (`migrations/env.py::get_url`): tries `app.config.settings.database_url` first; on any failure (e.g. Supabase creds absent) falls back to the raw `DATABASE_URL` env var. Either way the driver is normalized to `postgresql+asyncpg://`. Neither set → `RuntimeError: DATABASE_URL is not set`. So migrations work in credential-free contexts (CI, local Docker) with just `DATABASE_URL` exported.
- **Hand-written only.** `target_metadata = None` in `migrations/env.py` — there are no ORM models to autogenerate from. Every migration is raw `op.execute(...)` SQL in `migrations/versions/` (currently `0001`–`0008`; `script_location = migrations` in `alembic.ini`).
- **Down/up must round-trip** (`apps/api/CLAUDE.md`). Every `downgrade()` must actually revert its `upgrade()`, and `upgrade → downgrade → upgrade` must succeed cleanly. The `tests/test_migrations*.py` suites exercise downgrades against local Docker Postgres. Known flake: `test_downgrade_removes_table` occasionally deadlocks dropping RLS policies (#174, open) — a deadlock there is likely the flake, not your migration; see `stoop-debugging-playbook`.
- **Schema doc first.** A new table/column means editing `docs/03-engineering/schema-v1.md` **before** writing the migration (project rule 6). Names come from that doc; never invent them.
- **Pooler knobs.** `migrations/env.py` passes the same three asyncpg connect args as the app engine (`_ASYNCPG_POOLER_CONNECT_ARGS`: `prepared_statement_cache_size=0`, uuid4 `prepared_statement_name_func`, `statement_cache_size=0`). All three are required against Supabase's transaction pooler (port 6543) and harmless locally. Never remove any of them — the incident record is in `stoop-failure-archaeology`.
- **The Docker image contains no migrations.** The `Dockerfile` copies only the venv and `app/` — no `migrations/`, no `alembic.ini`. Migrations run from a git checkout, not from the deployed container.

## 3. Live Supabase operations: the dry-run discipline

**Standing rule (founder-elevated, never-break): any migration touching roles, grants, or RLS gets a dry-run against the LIVE Supabase database before merge** (session-verified 2026-07-05; no repo artifact — but the incident that created the rule is documented in migration `0004`'s docstring, which failed live with `must be able to SET ROLE` after passing local Docker green).

Why local green proves nothing here: local `docker compose` Postgres runs your migration as a bootstrap **superuser**, which is structurally blind to privilege errors. The three live-probed Supabase privilege traps (postgres-not-superuser, the `pg_has_role` MEMBER trap, the `GRANT … TO CURRENT_USER` connection kill) are cataloged in `stoop-domain-reference` §Supabase pack and in the `0004`/`0005` migration docstrings ("LIVE ROLE FACTS") — the operational consequence is this section: live dry-run before merge, every time. Incident history: `stoop-failure-archaeology`.

### The protocol

1. **Announce to the founder** what you are about to run against the live DB and why. Wait for acknowledgment if the thread has no standing directive covering it.
2. Export the live `DATABASE_URL` using the safe `.env` pattern (Python parses `apps/api/.env`, `shlex.quote`s values, `eval "$(…)"` consumes the export lines — never `source .env`, never print values; full pattern and its incident in `stoop-build-and-env` §5). Keep export + command on ONE shell line — env does not survive across tool calls.
3. Run `uv run alembic upgrade head` from `apps/api`.
4. **Verify**: query the objects the migration claims to create/alter (psql or a one-off `uv run python` snippet on the same connection string). Do not trust "no error".
5. **Round-trip where safe**: `uv run alembic downgrade -1` then `uv run alembic upgrade head` again. Skip the downgrade leg only when it would destroy live data or drop roles/grants something live depends on — say so explicitly in your report.
6. **Report** results (revisions applied, objects verified, any divergence from local behavior) before merging the PR.

Current live state (as of 2026-07-05, session-verified; no repo artifact): project ref `kytqtdqmzwyhiwkafcbh`, region `ca-central-1` (Canadian data residency is a requirement — a us-west-2 project was deleted and recreated over this), Postgres 17.6, migrations at head `0008`.

## 4. Operator flips — HUMAN-run; document, never execute

These involve secrets an agent must never generate, see, or set. Your job is to point the operator at the exact steps and verify the observable after-effects.

### 4a. app_role LOGIN + APP_DATABASE_URL (activates request-path RLS role separation)

Not yet done as of 2026-07-05 — `app_role` is still `NOLOGIN` (session-verified; no repo artifact). Until flipped, the request engine falls back to the admin engine with the one-time boot WARNING (§1). Production **refuses to boot** without it (`config.py::_require_app_database_url_in_production`). The canonical steps live in `app/db/session.py`'s module docstring:

1. Migration 0005 already created `app_role` as `NOLOGIN` — no password ever appears in a migration.
2. Operator runs once, directly against the live DB: `ALTER ROLE app_role LOGIN PASSWORD '<freshly generated secret>';`
3. Operator sets `fly secrets set APP_DATABASE_URL=postgresql+asyncpg://app_role.<project-ref>:<password>@<same pooler host>:6543/postgres`.
4. Redeploy. The lifespan self-check (§1) now proves the separation on every boot.

**Timing matters:** do this BEFORE real tenant data exists. Flipping role separation on after data has flowed through the admin-engine fallback is a migration project, not a config change (`app/db/session.py` docstring).

### 4b. PUBLIC_BASE_URL (proxy-aware Twilio signature verification)

Set to the exact public HTTPS origin Twilio POSTs webhooks to. When set, `app/integrations/twilio.py::reconstruct_signing_url` builds the signed URL from it; when unset (local-dev default) it falls back to `request.url` honoring `X-Forwarded-Proto`/`X-Forwarded-Host` — acceptable only behind a single trusted proxy hop. Production boot requires it (`_require_public_base_url_in_production`).

## 5. Webhook operations

Endpoints: `POST /webhooks/twilio/sms` (#40) and `POST /webhooks/twilio/status` (#152), `app/routers/webhooks/twilio.py`. No `Authorization` header — every request is verified against Twilio's HMAC-SHA1 `X-Twilio-Signature` (`app/integrations/twilio.py`: `compute_signature` / `verify_signature` / `reconstruct_signing_url`). Verification is **fail-closed**: bad or missing signature is rejected before anything touches the database. Both endpoints run on the admin engine (`get_admin_session`) by design — there is no landlord JWT to scope RLS by, and an RLS-scoped session could silently drop an inbound "there is a fire!" (never-break rule 1). The allowlist test `tests/test_migrations_0005.py::test_get_admin_session_referenced_only_by_allowlisted_files` enforces that this stays the exception.

Operating rules (the router's module docstring is the full contract):

- **Commit-first.** The message INSERT commits in its own transaction before any side effect runs. Post-persist side effects each run in their own isolated session and are idempotent via a real Postgres unique index (`uq_notifications_message_dedupe`, migration 0006) — safe under concurrent redelivery storms.
- **`/sms` returns 5xx on recovery failure BY DESIGN.** If a redelivery's recovery lookup fails, the 5xx tells Twilio to retry — Twilio's at-least-once retry IS the recovery mechanism. Never "fix" that 5xx into a 200; an earlier revision did exactly that and could permanently strand a hard-fire message's escalation artifacts.
- **Dedupe on `twilio_sid`.** `messages.twilio_sid` is UNIQUE (migration 0002); the INSERT uses `ON CONFLICT`. Replays no-op on the message and re-run the idempotent side effects.
- **`/status` is append-only.** Every callback appends to `message_status_events` — duplicates and out-of-order arrivals included; each is a fact. Always answers 200 once the signature is valid (any non-2xx makes Twilio retry-storm).

**Local testing:** you cannot curl these endpoints bare — the signature check rejects you. Compute a real signature with `app.integrations.twilio.compute_signature(url, params, settings.twilio_auth_token)`, exactly as the test suites do (`tests/test_webhooks_twilio_sms.py::_sign`, `tests/test_webhooks_twilio_status.py`). Prefer extending those tests over ad-hoc curling.

## 6. Eval-run operations (paid, founder-gated)

**Paid eval runs need the founder's go-ahead and are launched by the orchestrator only** — never by an implementer agent unilaterally (founder-elevated rule, session-verified 2026-07-05; the runner's own docstring repeats the orchestrator-only half). Both `python -m evals.runner` (without `EVAL_DRY_RUN=1`) and `uv run pytest -m eval` hit the real Anthropic API. Note bare `uv run pytest` also COLLECTS the paid `eval` marker — the safe default everywhere is `-m "not eval"` (see `stoop-build-and-env` §6).

Free harness exercise (no API calls, safe for anyone):

```bash
cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner
```

Real run — orchestrator only, after founder go-ahead, in the background (paid; current cost/duration figures live in `stoop-change-control` rule 9 — per-call cost tracking is `estimate_cost_cents` in the runner). Use the safe `.env` export (§3 step 2 / `stoop-build-and-env` §5) on ONE shell line, capture output to a log, and never mask the exit code with a pipe:

```bash
eval "$(python3 - <<'PY'
import shlex
with open("apps/api/.env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        print(f"export {key.strip()}={shlex.quote(value.strip())}")
PY
)" && cd apps/api && uv run python -m evals.runner > /tmp/eval-run.log 2>&1; echo EXIT=$?
```

Run that as a background task; poll the log. Exit code 0 = gate passed; 1 = `release_blocked` (any E-class/F-class or Tier-0 assertion failure). Use THIS exit code — not pytest's — to decide whether a prompt/rubric change is mergeable; the runner encodes the hard/soft distinction (`evals/scoring.py`), pytest reds on any failure including soft U/R misses.

What a full run is: 20 scenarios — 11 LLM scenarios (`evals/scenarios/*.yaml`: e1–e4, f1–f2, u1–u3, r1–r2) + 9 Tier-0-only negatives (`evals/scenarios/negative_prefilter/n1–n9`; skip with `--no-negative`), 3 classification samples per LLM scenario (`CLASSIFICATION_SAMPLES_PER_SCENARIO`), 1 draft + 1 judge call each. Pacing/backoff knobs (all in `evals/runner.py`): `EVAL_TOKEN_BUDGET_PER_MIN` (default 25000, 60s sliding window), `EVAL_PACE_FLOOR_SECONDS` (default 1.0), backoff capped at 70s, max 6 retries, retryable only on rate-limit/overload. An infra failure marks a scenario INCONCLUSIVE (`ScenarioInfraError`) rather than a rubric miss — but still sets `release_blocked`; re-run.

Results conventions:

- `evals/results/last-run.json` — **always** written, every invocation, even crash paths. Gitignored; never commit it.
- `evals/results/v1-baseline.json` — written only with `--snapshot` (or `EVAL_WRITE_SNAPSHOT=1`). The gate-9 GREEN baseline (20/20, `release_blocked=False`, 2026-07-06) is COMMITTED at this path via a root-`.gitignore` exception (`!apps/api/evals/results/v1-baseline.json`, commit `7fe8609`) — the one deliberate exception to "never commit result JSONs"; don't overwrite it without founder sign-off.
- Judge-fail triage rule: when the judge fails a draft, cross-check `judge_reasoning` prose against the boolean checklist in `last-run.json` — disagreement means an eval-infra bug, not a product bug (see `stoop-failure-archaeology` for the inversion incident).

## 7. Deploy state (as of 2026-07-05)

| Piece | State |
|---|---|
| `apps/api/Dockerfile` | Ready. Multi-stage (uv builder → slim runtime), non-root user `app` (uid 1000), `EXPOSE 8080`, `CMD uvicorn app.main:app --host 0.0.0.0 --port 8080`. Build context is `apps/api`. Image contains ONLY the venv + `app/` — no migrations, no tests. |
| `fly.toml` | **Does not exist anywhere in the repo.** Fly deploy is issue #13, open, human-blocked (account/secrets) (session-verified 2026-07-05; no repo artifact). |
| Web deploy | Cloudflare Workers via wrangler (`apps/web/wrangler.jsonc`). The D1 binding's `database_id` is the placeholder `REPLACE_WITH_D1_ID` — a human must run `bunx wrangler d1 create stoop-waitlist` and paste the id first (comment in the file itself). |

What lands where at runtime:

- **Logs:** structlog single-line JSON to stdout (`app/observability.py::configure_logging`) — Fly/container-native. No JWTs, phone numbers, or message bodies, ever (project rule 5).
- **Sentry:** only when `SENTRY_DSN` is set; `send_default_pii=False` + `include_local_variables=False` are non-negotiable.
- **Agent decisions:** the canonical classification record is an `audit_log` row (`actor='agent'`, `action='classified'`) — never columns on `messages` (`app/agent/nodes/classify_severity.py` docstring; `messages` and `audit_log` are append-only, project rule 2).

## 8. Humans-only: stop and ask

Agents never attempt these — if a task needs them, stop and tell the founder exactly what is needed (`apps/api/CLAUDE.md` "Things humans must do"):

| Category | Examples |
|---|---|
| Account creation | Supabase, Twilio, LangSmith, Sentry, Fly, PostHog, Stripe |
| Secrets | `fly secrets set …`, the `app_role` password (§4a), rotating any leaked credential |
| Regulatory filings | A2P 10DLC registration, CASL compliance |
| Billing | Stripe dashboard products/prices |
| DNS / domains | Any registrar or DNS change |
| Platform buttons | `bunx wrangler d1 create stoop-waitlist`, Fly app creation (#13), GitHub plan/branch-protection settings |

## Provenance and maintenance

| Claim | Re-verify with |
|---|---|
| Startup order + debug-router gating | `grep -n "include_router\|is_production" apps/api/app/main.py` |
| Required config fields + production boot gates | `grep -n "_require_.*_in_production\|: str = Field" apps/api/app/config.py` (or read `app/config.py`) |
| Lifespan = role-check then checkpointer, fail-closed | `sed -n '31,60p' apps/api/app/main.py` |
| Checkpointer knobs (`prepare_threshold: None`, autocommit) | `grep -n "prepare_threshold\|autocommit" apps/api/app/agent/checkpointer.py` |
| Migration URL fallback + driver normalization | `grep -n "def get_url" -A 35 apps/api/migrations/env.py` |
| `target_metadata = None` (no autogenerate) | `grep -n "target_metadata" apps/api/migrations/env.py` |
| Three asyncpg pooler knobs in migrations | `grep -n "_ASYNCPG_POOLER_CONNECT_ARGS" -A 5 apps/api/migrations/env.py` |
| Migration head (currently 0008) | `ls apps/api/migrations/versions/` |
| Live Supabase role facts | `grep -n "LIVE ROLE FACTS" -A 10 apps/api/migrations/versions/0005_app_role_and_rls.py` |
| Operator flip steps (app_role LOGIN) | `sed -n '1,50p' apps/api/app/db/session.py` |
| `/sms` 5xx-by-design + commit-first contract | `sed -n '1,120p' apps/api/app/routers/webhooks/twilio.py` |
| `messages.twilio_sid` UNIQUE | `grep -n "twilio_sid" apps/api/migrations/versions/0002_core_schema.py` |
| Eval results paths + always-written last-run | `grep -n "LAST_RUN_PATH\|SNAPSHOT_PATH" apps/api/evals/runner.py` |
| Eval pacing constants | `grep -n "EVAL_TOKEN_BUDGET_PER_MIN\|RATE_LIMIT_BACKOFF_CAP\|RATE_LIMIT_MAX_RETRIES\|PACE_FLOOR" apps/api/evals/runner.py` |
| Scenario counts (11 LLM + 9 negative) | `ls apps/api/evals/scenarios/ apps/api/evals/scenarios/negative_prefilter/` |
| last-run gitignored; v1-baseline committed via exception | `grep -n "evals/results" .gitignore` (repo root — expect the ignore AND the `!…v1-baseline.json` exception) |
| Dockerfile port/user/contents | `cat apps/api/Dockerfile` |
| fly.toml still absent | `find . -maxdepth 3 -name fly.toml` (repo root) |
| Wrangler D1 placeholder | `grep -n "database_id" apps/web/wrangler.jsonc` |
| Live project ref / region / PG version / head (volatile, session-sourced) | Ask the founder or check the Supabase dashboard; then `alembic current` via the live-URL pattern in §3 |
