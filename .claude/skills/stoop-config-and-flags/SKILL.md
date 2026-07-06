---
name: stoop-config-and-flags
description: >
  Every configuration axis of the Stoop monorepo (apps/api FastAPI backend +
  apps/web TanStack Start site). Load this when you need to: add/change/read an
  environment variable or Settings field; understand why the app refuses to
  boot in production (APP_DATABASE_URL / PUBLIC_BASE_URL gates); tune or
  understand eval-harness knobs (EVAL_DRY_RUN, EVAL_TOKEN_BUDGET_PER_MIN,
  EVAL_PACE_FLOOR_SECONDS, EVAL_RATE_LIMIT_MAX_RETRIES, EVAL_WRITE_SNAPSHOT);
  look up a hardcoded constant (LLM timeouts, model id, cost table, JWKS TTL
  and cooldowns, weather cache, DB pool sizes, case-lifecycle windows,
  prompt/rubric/prefilter versions, draft length budgets); configure the web
  app (wrangler.jsonc D1 binding, VITE_PLAUSIBLE_DOMAIN, bunfig
  minimumReleaseAge); or reason about feature flags (PostHog flags are planned,
  not wired; flags never gate safety). Also covers the conftest placeholder-env
  mechanism that makes unit tests run with zero real credentials, and the
  step-by-step checklist for adding a new config axis.
---

# Stoop configuration and flags

Repo root: the Stoop monorepo (`apps/api` = Python 3.12/FastAPI backend,
`apps/web` = TanStack Start site on Cloudflare Workers). All commands below are
repo-relative. "Settings" means the Pydantic-settings class in
`apps/api/app/config.py`; "boot gate" means a validator that makes the process
refuse to start.

## When NOT to use this skill

| You actually want | Go to |
|---|---|
| Recreate the dev environment (uv, Docker Postgres, bun) from scratch | `stoop-build-and-env` |
| Run the server, run migrations, do the one-time `app_role LOGIN` operator flip | `stoop-run-and-operate` |
| Eval discipline, what counts as passing evidence, the golden test inventory | `stoop-validation-and-qa` |
| Why a config gate exists — the incident history behind it | `stoop-failure-archaeology` |
| How to get a config change merged (reviewers, gates) | `stoop-change-control` |
| Load-bearing design invariants (append-only tables, RLS design) | `stoop-architecture-contract` |
| Diagnosing a failure symptom (401 flakes, pooler errors) | `stoop-debugging-playbook` |

This skill answers "what are the knobs, what are their values, and how do I add
one" — not "how do I ship it" and not "what went wrong".

## Config architecture in 60 seconds

- `apps/api/app/config.py` declares every env var as a typed Pydantic field on
  `Settings` (`pydantic-settings`, `env_file=".env"`, `case_sensitive=False`,
  `extra="ignore"`). A module-level singleton `settings` is constructed at
  import time — a missing required var raises `pydantic.ValidationError`
  before the app can start. `get_settings()` is `@lru_cache`d; tests call
  `get_settings.cache_clear()` to force a re-read.
- Never log or print the `settings` object — it carries secrets (module
  docstring says so explicitly).
- `apps/api/.env.example` is the safe, commented template. `.env` itself is
  gitignored, holds live credentials, and must never be read, `cat`ed, or
  `source`d (a `source .env` under zsh once echoed the live Twilio auth token
  into a terminal log and forced a rotation — session-verified 2026-07-05; no
  repo artifact; full story in `stoop-failure-archaeology`). Parse it with
  Python if you must, printing nothing.
- Everything that is not an env var is a named module constant, cataloged in
  the "Constants catalog" section below.

## Environment variables — complete table (apps/api)

Source of truth: `apps/api/app/config.py` + `apps/api/.env.example`. Verified
field-by-field against both (as of 2026-07-05).

| Env var | Required? | Default | Consumed by | Production boot gate? |
|---|---|---|---|---|
| `ENVIRONMENT` | no | `"dev"` | `settings.is_production` checks in `app/main.py`, `app/observability.py`, `app/routers/debug.py` | It is the gate *trigger*: only `Literal["dev","staging","production"]` accepted (typo = startup error) |
| `LOG_LEVEL` | no | `"INFO"` | `app/observability.py::configure_logging` | no |
| `DATABASE_URL` | **yes (import-time)** | — | `app/db/session.py` (admin engine, health checks), `app/agent/checkpointer.py` (dedicated psycopg pool), `migrations/env.py` (Alembic) | no |
| `APP_DATABASE_URL` | no | `None` | `app/db/session.py` request engine (`get_session`) when set; unset = request sessions fall back to the admin engine + one startup WARNING that RLS role separation is not enforced | **YES** — `_require_app_database_url_in_production` refuses to boot when `ENVIRONMENT=production` and this is unset (#22) |
| `SUPABASE_URL` | **yes (import-time)** | — | declared in `app/config.py` only; no consumer module yet (as of 2026-07-05) | no |
| `SUPABASE_JWKS_URL` | **yes (import-time)** | — | `app/integrations/supabase_auth.py` (JWKS fetch for JWT verification) | no |
| `SUPABASE_JWT_ISSUER` | **yes (import-time)** | — | `app/integrations/supabase_auth.py` (expected `iss` claim) | no |
| `SUPABASE_SERVICE_ROLE_KEY` | **yes (import-time)** | — | declared in `app/config.py` only; no consumer module yet (as of 2026-07-05) | no |
| `TWILIO_AUTH_TOKEN` | **yes (import-time)** | — | `app/routers/webhooks/twilio.py` (X-Twilio-Signature verification; also the HMAC key for signature-log truncation) | no |
| `PUBLIC_BASE_URL` | no | `None` | `app/routers/webhooks/twilio.py` via `reconstruct_signing_url` (`app/integrations/twilio.py`); unset = falls back to `X-Forwarded-Proto`/`X-Forwarded-Host` (local-dev only) | **YES** — `_require_public_base_url_in_production` refuses to boot when `ENVIRONMENT=production` and this is unset (#40/#152): webhook signature verification must not trust proxy headers in production |
| `ANTHROPIC_API_KEY` | **yes (import-time)** | — | `app/integrations/anthropic.py::get_client` | no |
| `LANGSMITH_API_KEY` | no | `None` | `app/observability.py::init_langsmith_tracing` — when set, exports `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` for the langsmith SDK; when unset, nothing is exported and no tracing is attempted | no |
| `LANGSMITH_PROJECT` | no | `None` | `app/observability.py` (only meaningful with the API key; SDK falls back to its own "default" project) | no |
| `SENTRY_DSN` | no | `None` | `app/observability.py::init_sentry` — unset/blank disables Sentry entirely. Init hardcodes `send_default_pii=False` AND `include_local_variables=False` (both mandatory: frame locals can hold JWTs) plus a `before_send` scrubber | no |

Notes on the two boot gates (they are twins — same pattern, different field):

- Both fields have a `mode="after"` field validator that normalizes a
  whitespace-only value to `None`, so a blank Fly secret cannot "sail past"
  the gate and die later inside `create_async_engine` with an obscure error.
- Both gates fire only on ABSENCE, so the error message never contains a
  secret (`tests/test_config.py` asserts `"postgresql" not in message`).
- The `APP_DATABASE_URL` flip requires a one-time operator step (`ALTER ROLE
  app_role LOGIN PASSWORD ...` run directly against the database, never in a
  migration) — see `app/db/session.py`'s module docstring and
  `stoop-run-and-operate`. As of 2026-07-05 that flip has NOT been done on the
  live project; `app_role` is still NOLOGIN (session-verified 2026-07-05; no
  repo artifact).

### The 7 import-time-required vars and how unit tests stay env-free

`apps/api/tests/conftest.py` sets placeholder values via
`os.environ.setdefault` for exactly the 7 required fields — `DATABASE_URL`,
`SUPABASE_URL`, `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER`,
`SUPABASE_SERVICE_ROLE_KEY`, `TWILIO_AUTH_TOKEN`, `ANTHROPIC_API_KEY` — BEFORE
any test module imports `app.config`. Consequences:

1. Unit tests need zero real credentials and zero `.env` file.
2. `setdefault` means a real exported env var always wins over the placeholder.
3. Integration tests (marker `integration`) need a real database: export
   `DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop'` (the
   root `docker-compose.yml` Postgres) IN THE SAME shell invocation as pytest.
   If you forget, they silently hit the placeholder
   `postgresql+asyncpg://test:test@localhost:5432/test` and produce dozens of
   connection ERRORs that look like real breakage — this false alarm burned
   multiple sessions ("204 errors" scares; session-verified 2026-07-05, the
   placeholder mechanism itself is repo-verifiable in `tests/conftest.py`).
4. `pyproject.toml` has markers but NO `addopts` guard, so bare
   `uv run pytest` USED to collect the paid `@pytest.mark.eval` test; since PR #177 `pyproject.toml` carries `addopts = "-m 'not eval'"` so the bare command is safe, and an explicit `-m eval` overrides it (last -m wins). Safe
   default: `cd apps/api && uv run pytest -m "not eval"`.

## Eval-harness env knobs (apps/api/evals/runner.py)

The eval harness runs the 20-scenario corpus (11 LLM scenarios + 9
Tier-0-only negatives in `evals/scenarios/`) against the real Anthropic API.
A real run is paid and founder-gated — current cost/time figures live in
`stoop-change-control` rule 9 — and is the orchestrator's call with the
founder's go-ahead, never something an agent fires unilaterally (the runner
prints a warning banner when `EVAL_DRY_RUN` is unset). Each knob below was
added in response to a specific real-gate failure, documented in the module
docstring of `evals/runner.py`:

| Knob | Default | What it does | Incident rationale |
|---|---|---|---|
| `EVAL_DRY_RUN` | unset | `=1` swaps the real transport for a deterministic per-scenario stub (`make_dry_run_tool_caller`): zero network, zero cost, exercises the full loader→prompt→assertion→scoring→reporting pipeline. Anything else = REAL, PAID API calls | The only safe way for a non-orchestrator to touch the runner. `tests/test_evals.py` uses it throughout |
| `EVAL_TOKEN_BUDGET_PER_MIN` | `25000` | Sliding-60s-window budget of ACTUAL input tokens; before each call, sleeps until trailing sum + a per-tool estimate (classify ~4500 / draft ~2000 / judge ~1500) fits | Round 1's flat 1.5s pacing still failed 11/20 on a Tier-1 Anthropic key: Tier-1 caps INPUT TOKENS per minute (~30–50k), not just request rate, and the classify system prompt (rubric embedded verbatim) is ~4k tokens alone. 25000 = conservative headroom under that diagnosed cap |
| `EVAL_PACE_FLOOR_SECONDS` | `1.0` | Small always-applied sleep before every real call, independent of token accounting — a request-rate courtesy | Survivor of the round-1 flat-pacing approach; kept as a floor after token budgeting became the real mechanism |
| `EVAL_RATE_LIMIT_MAX_RETRIES` | `6` | Max backoff retries, ONLY for `anthropic.RateLimitError` (429) / `anthropic.OverloadedError` (529) detected via `exc.__cause__` on `AnthropicCallError` | Round 2 raised 5→6. Deliberately narrow: retrying an auth/bad-request error would waste the whole budget on something that can never succeed |
| `EVAL_WRITE_SNAPSHOT` | unset | `=1` (or `--snapshot`) additionally writes `evals/results/v1-baseline.json` — the gate-9 GREEN baseline (20/20, 2026-07-06) is COMMITTED at that path via a root-`.gitignore` exception (commit `7fe8609`); don't overwrite it casually. `evals/results/last-run.json` is ALWAYS written regardless, even on crash paths | The first paid-gate diagnosis lost per-sample failure detail to a `\| tail -8` console truncation; the always-written report makes that impossible to repeat |
| backoff base/cap (constants, not env) | `RATE_LIMIT_BACKOFF_BASE_SECONDS=2.0`, `RATE_LIMIT_BACKOFF_CAP_SECONDS=70.0` | Exponential 2/4/8/16/32/64s, capped at 70s | Round 2 raised the cap 30s→70s: the token window refills every 60s, so a full refill must fit inside ONE backoff step; no ceiling under 60s can ever outlast it |

Non-negotiable semantics baked into the runner (do not "fix" them):

- A 429 retry is NOT a 4th classification sample — the flaky-fails rule
  polices model variance across calls that each produced an answer; a
  rate-limited attempt produced nothing. Semantic retries (re-calling because
  the answer looked wrong) are forbidden everywhere.
- Infra failure ≠ semantic failure: exhausted backoff or malformed output
  raises/records `ScenarioInfraError` → scenario is INCONCLUSIVE (re-run),
  never a rubric miss — but it still sets `release_blocked`.
- Use `python -m evals.runner`'s exit code (not raw pytest) to decide
  mergeability: it encodes hard-fail (E/F-class or any Tier-0 assertion)
  vs soft-fail (U/R-class) per the eval doc.
- Free harness check: `cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner`

## Constants catalog (file:symbol | value | meaning | why that value)

All verified in-source (as of 2026-07-05). Paths relative to `apps/api/`.

### LLM call budget and pricing — `app/integrations/anthropic.py`

| Symbol | Value | Meaning / why |
|---|---|---|
| `MODEL` | `"claude-sonnet-5"` | The one model id for every agent LLM call |
| `CLASSIFICATION_BUDGET_SECONDS` | `20.0` | End-to-end budget shared across first attempt + retry (the "20s classification budget" in `apps/api/CLAUDE.md`) |
| `FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS` | `12.0` | First attempt capped so a retry still has meaningful time left |
| `MIN_RETRY_BUDGET_SECONDS` | `2.0` | Below this remainder, no retry is attempted (not worth it) |
| `max_retries=0` (in `get_client`) | `0` | The SDK's hidden default is 2 retries; that would layer UNDER the product's own retry-once policy. The product owns retries end-to-end (senior review, 2026-07-05) |
| temperature | OMITTED entirely | `temperature` is deprecated on claude-sonnet-5 — the API 400s if you pass it. Determinism is owned by the eval gate (3 samples, flaky = fail). Tests assert the parameter's ABSENCE. Do not re-add it |
| `_INPUT_PRICE_PER_MTOK_USD` / `_OUTPUT_PRICE_PER_MTOK_USD` | `3.00` / `15.00` | Cost table for `estimate_cost_cents`. **PLACEHOLDER** — mirrors published Sonnet-tier pricing; no confirmed claude-sonnet-5 rate existed at authoring time. Reconcile with real invoices when billing data exists (see `_PRICING_SOURCE_NOTE`) |

### Draft length budgets

| Symbol | Value | Meaning |
|---|---|---|
| `app/agent/nodes/draft_response.py::_LENGTH_BUDGET_CHARS` | `300` | ~2 SMS segments (plain-language rule 5). Refusal-flagged drafts are exempt: the code-appended deferral template legitimately makes them longer. Truncation is forbidden — over-budget-but-guard-clean drafts are kept and flagged |
| `evals/scoring.py::_ROUTINE_LENGTH_BUDGET_CHARS` | `320` | Eval-side check = 300 + small slack for appended deferrals; `category == "refusal"` scenarios are exempted entirely |

### JWKS auth cache — `app/integrations/supabase_auth.py`

(JWKS = the JSON Web Key Set fetched from Supabase to verify JWT signatures.)

| Symbol | Value | Meaning |
|---|---|---|
| `_JWKS_TTL_SECONDS` | `86_400.0` (24h) | Cache lifetime for the fetched key set; stale-beyond-TTL keys are deliberately served during outages (availability over freshness) |
| `_FORCED_REFRESH_WINDOW_SECONDS` | `60.0` | Rate limit on unknown-`kid` forced refreshes (kid-miss path, #134) |
| `_DEGENERATE_FETCH_COOLDOWN_SECONDS` | `60.0` | Cooldown after a fetch that returned 200 but a degenerate/empty key set (#147 follow-up) — same value as above by coincidence, independent knob |
| `_FETCH_EXCEPTION_COOLDOWN_SECONDS` | `5.0` | Cooldown after a confirmed fetch exception (#158) — prevents hammering Supabase once per request on a cold/expired cache |

All state lives in one `_JwksState` object with `reset_for_tests()`;
`tests/conftest.py` resets it autouse before EVERY test (cross-event-loop Lock
reuse and leftover cooldown stamps caused the #141-class 401 flakes). Note:
`supabase_auth.py` has in-flight edits on branch `fix/jwks-refetch-on-kid-miss`
(as of 2026-07-05); the four constants above are identical on `main`.

### Weather integration — `app/integrations/weather.py`

| Symbol | Value | Meaning |
|---|---|---|
| `_TIMEOUT_SECONDS` | `3.0` | Hard fetch budget — weather context must never meaningfully delay classification; the function never raises |
| `_CACHE_TTL_SECONDS` | `30 * 60` (1800) | In-process TTL cache; the AC's freshness bound (shorter violates the AC, much longer goes stale) |
| `_HEAT_WARNING_MAX_TEMP_C` | `31.0` | `heat_warning=True` when today's forecast daily high >= 31.0 C (matches the rubric's documented threshold) |

### Database engines — `app/db/session.py`, `app/agent/checkpointer.py`

| Symbol / kwarg | Value | Meaning |
|---|---|---|
| SQLAlchemy engines: `pool_size` / `max_overflow` | `5` / `5` | Modest steady pool, burst to 10 total; applies to BOTH the admin engine and the (optional) request engine |
| `pool_pre_ping` | `True` | ~1ms per checkout to detect stale sockets |
| `pool_recycle` | `300` | Recycle connections every 5 min (shorter than pooler idle timeouts) |
| `_ASYNCPG_POOLER_CONNECT_ARGS` | `prepared_statement_cache_size=0` + uuid4 statement-name func + `statement_cache_size=0` | The THREE Supavisor transaction-pooler knobs. The documented 2-knob recipe was insufficient — `pool_pre_ping`'s ping bypasses the dialect and uses asyncpg's own cache; the third knob closed an 18/100 live failure rate to 0/100 (PR #165). Same knobs duplicated in `migrations/env.py`. NEVER remove any of the three |
| checkpointer pool: `min_size` / `max_size` | `1` / `5` | Dedicated psycopg (NOT asyncpg) pool for the LangGraph checkpointer only |
| checkpointer `prepare_threshold` | `None` | psycopg's pooler-compat equivalent. `None`, NOT `0` — `0` means prepare-everything |
| checkpointer `autocommit` | `True` | `AsyncPostgresSaver.setup()` runs `CREATE INDEX CONCURRENTLY`, which refuses to run inside a transaction block; `search_path` is pinned twice (startup-packet `options` + `configure` callback) |

### Case lifecycle windows — `app/agent/case_lifecycle.py` + schema doc

| Symbol | Value | Meaning |
|---|---|---|
| `RESOLUTION_PROPOSAL_WINDOW` | `timedelta(hours=48)` | Tenant-confirmed resolution auto-applies 48h after proposal unless the landlord acts; takes precedence over auto-stale |
| `AUTO_STALE_INACTIVITY` | `timedelta(days=14)` | Auto-stale after 14d of no `last_activity_at`; boundary INCLUSIVE (exactly 14d fires); `awaiting_approval` cases are excluded |
| `REOPEN_WINDOW` | `timedelta(days=30)` | Tenant message about a resolved case reopens it if resolved <= 30d ago (inclusive); older spawns a NEW case with `related_case_id` |
| Approve undo window | 5 seconds (dashboard) | `docs/03-engineering/schema-v1.md`: approve sets `drafts.scheduled_send_at = now() + 5s`; the sender only sends approved rows whose time has come — "the undo window is data, not a sleep". The #44 approve endpoint is UNBUILT as of 2026-07-05 (SMS-approve variant `now() + 5 minutes` per #122, also unbuilt) |

### Versioned safety artifacts

| Symbol | Value | Change discipline |
|---|---|---|
| `app/agent/rubric.py::RUBRIC_VERSION` | `"1.0"` | Rubric text is byte-identical to `docs/02-product/severity-rubric-v1.md` (checksum test). Change = new version file + full eval run — see `stoop-change-control` |
| `app/agent/prompts/v1.py::PROMPT_VERSION` | `"v1"` | FROZEN HISTORY — the v1 baseline package; never edit it |
| `app/agent/prompts/v2.py::PROMPT_VERSION` | `"v2"` | **LIVE on `main`** (merged in PR #177, 2026-07-06): founder-approved templates-only bump (commit `11564c8` + pre-merge amendment `31bd498`, which dropped the word "soon"; refusal templates rewritten plain-language, system prompts re-exported from frozen v1 by construction). Verified by eval gate 9 — 20/20, `release_blocked=False` (2026-07-06). Consumers (`classify_severity.py`, `draft_response.py`, `evals/runner.py`) import `prompts.v2`; drafts/audit rows stamp `prompt_version="v2"` — a v2-stamped `last-run.json` is CORRECT, not drift. Never edit an existing version; the next free slot is v3. Diff pinned by `tests/test_agent_schemas.py::test_prompts_v2_changes_exactly_the_founder_approved_templates` |
| `app/agent/prefilter.py::PREFILTER_VERSION` | `"1.0"` | Tier-0 pattern changes are monotonic ADDITIVE only, each with a regression test class in `tests/test_prefilter.py` (#144 discipline) |

## Web config (apps/web)

| File | Axis | Current state (as of 2026-07-05) |
|---|---|---|
| `apps/web/wrangler.jsonc` | Cloudflare Worker config: `name: "stoop-web"`, `compatibility_date: "2025-09-24"`, `nodejs_compat`, `main: src/server.ts`. D1 (Cloudflare's SQLite) binding `WAITLIST_DB` → database `stoop-waitlist`, `migrations_dir: "migrations"` | **`database_id` is the literal placeholder `REPLACE_WITH_D1_ID`** — a real deploy first needs `bunx wrangler d1 create stoop-waitlist` and the resulting id pasted in |
| `VITE_PLAUSIBLE_DOMAIN` (build-time env) | Read in `apps/web/src/routes/__root.tsx`; the Plausible analytics script tag is injected ONLY when set, served same-origin per ADR-5 (no direct plausible.io script). `src/lib/analytics.ts::trackEvent` is a never-throws no-op wrapper; props must never carry PII | No production domain yet, so unset everywhere — analytics is currently a silent no-op |
| `apps/web/bunfig.toml` | Supply-chain guard: `minimumReleaseAge = 86400` — bun refuses package versions published < 24h ago | One exclusion exists (`@lovable.dev/vite-tanstack-config`); the file's own comment requires confirming with the user before adding any more |
| `apps/web/vite.config.ts` | Wraps `defineConfig` from `@lovable.dev/vite-tanstack-config`, which already bundles tanstackStart/viteReact/tailwind/cloudflare/`VITE_*` injection/path aliases | Do NOT re-add those plugins manually (duplicate-plugin breakage — the file's header comment); server entry redirected to `src/server.ts` |

## Feature flags doctrine

- **State (as of 2026-07-05): there are NO feature flags wired anywhere.**
  PostHog server-side flags are PLANNED under issue #121 (`feat(flags):
  PostHog feature flags server-side — rollouts + pricing cohorts`, OPEN on
  `LaithAlz/stoop-backend`). `app/integrations/posthog.py` appears in
  `apps/api/CLAUDE.md`'s target layout but does not exist yet; there is no
  posthog import in `apps/api/app/`.
- **The never-break rule** (root `CLAUDE.md` rule 7 + `apps/api/CLAUDE.md`
  agent rules): flags gate rollouts, pricing cohorts, and experiments ONLY.
  Flags NEVER gate safety behavior — the emergency path, the rubric, or
  approval requirements. `app/integrations/anthropic.py`'s docstring already
  pre-commits to this ("No feature-flag reads here").
- **Failure semantics**: flag-service failure must be indistinguishable from
  flags-off. When #121 lands, the local-fallback path is a requirement, not an
  optimization — any design where a PostHog outage changes product behavior
  beyond "experiment cohorts collapse to default" is wrong.
- Analytics discipline rides along (ADR-5): PostHog identifies by landlord
  uuid only — no emails/names/phones/message bodies in event properties;
  session replay stays off.

## Adding a config axis — checklist

For a new ENV VAR / Settings field:

1. **`apps/api/app/config.py`**: add the typed field with a docstring
   description saying who consumes it and what unset means. Optional fields
   default to `None`; secrets get NO default. If a blank value must not
   sneak past validation, copy the `_normalize_app_database_url`
   whitespace-to-None field-validator pattern. If production must refuse to
   boot without it, copy the `_require_app_database_url_in_production`
   model-validator pattern (message must never contain a secret — it only
   fires on absence).
2. **`apps/api/.env.example`**: add the var with a comment block matching the
   file's existing style (what it is, when to leave it unset, where the real
   value comes from). Never put a real value here.
3. **`apps/api/tests/conftest.py`**: ONLY if the field is required
   (import-time), add a placeholder to `_PLACEHOLDER_ENV` — otherwise every
   unit test run breaks in CI.
4. **`apps/api/tests/test_config.py`**: add tests mirroring the existing
   pattern — default/carried-through for optional fields; missing-raises for
   required fields; the full gate quartet (unset-raises, whitespace-raises,
   set-succeeds, non-production-fine) for any new boot gate. Note that every
   production-mode `Settings(...)` construction in that file must now also
   pass your new required-in-production field, or existing tests start
   failing.
5. **Docs**: touch `docs/03-engineering/architecture.md` (or the owning doc)
   if the axis is contract-visible; new endpoints' env dependencies belong in
   the same PR as the endpoint per `apps/api/CLAUDE.md`. See
   `stoop-docs-and-writing` for amendment discipline.
6. **`CLAUDE.md`**: only if it is a gate or a never-break rule — CLAUDE.md is
   for rules, not inventory.
7. Ship through the normal `/ship` flow (`stoop-change-control`). Deployment
   secret-setting (`fly secrets set`) is a human step — agents stop and ask.

For a new eval knob: define the module constant in `evals/runner.py` reading
`os.environ.get` with a safe default, document the incident rationale in the
module docstring, export it in `__all__`, and cover it in
`tests/test_evals.py` via monkeypatch — never by running the paid gate.

**A new DATABASE COLUMN is a different animal entirely**: edit
`docs/03-engineering/schema-v1.md` FIRST (root `CLAUDE.md` rule 6 — schema
names come from that doc, never invented), then write the Alembic migration
(down/up must round-trip). If the migration touches roles/grants/RLS it
additionally needs a live Supabase dry-run before merge (standing rule; local
Docker runs as bootstrap superuser and is blind to privilege bugs — see
`stoop-failure-archaeology`). Config checklist above does not apply.

## Provenance and maintenance

Drift-prone claims and a one-line re-verification for each (run from repo
root unless noted; all are read-only and free):

| Claim | Re-verify with |
|---|---|
| Env-var table matches `Settings` | `grep -n "Field(\|: str\|: Literal\|None = " apps/api/app/config.py` |
| Still exactly 7 required placeholders | `grep -A9 "_PLACEHOLDER_ENV" apps/api/tests/conftest.py` |
| Boot gates unchanged | `grep -n "_require_.*_in_production" apps/api/app/config.py` |
| Eval knob defaults (25000 / 1.0 / 6 / 2.0 / 70.0) | `grep -n "EVAL_\|RATE_LIMIT_BACKOFF" apps/api/evals/runner.py \| head -20` |
| Model id + LLM budgets (20/12/2s, max_retries=0) | `grep -n "MODEL\|_SECONDS\|max_retries" apps/api/app/integrations/anthropic.py \| head` |
| Cost table still the $3/$15 placeholder | `grep -n "_PRICE_PER_MTOK\|PRICING_SOURCE" apps/api/app/integrations/anthropic.py` |
| Draft budgets 300/320 | `grep -rn "_LENGTH_BUDGET_CHARS\|_ROUTINE_LENGTH_BUDGET" apps/api/app/agent/nodes/draft_response.py apps/api/evals/scoring.py` |
| JWKS TTL + 3 cooldowns (86400/60/60/5) | `grep -n "_SECONDS: float" apps/api/app/integrations/supabase_auth.py` |
| Weather 1800/3.0/31.0 | `grep -n ": float" apps/api/app/integrations/weather.py \| head -5` |
| Pool sizes + 3 pooler knobs | `grep -n "pool_\|cache_size" apps/api/app/db/session.py \| head` |
| Lifecycle windows 48h/14d/30d | `grep -n "timedelta" apps/api/app/agent/case_lifecycle.py \| head -5` |
| Versions (rubric 1.0 / prompt v2 live, v1 frozen / prefilter 1.0) | `ls apps/api/app/agent/prompts/` then `grep -rn "_VERSION: str" apps/api/app/agent/{rubric,prefilter}.py apps/api/app/agent/prompts/v*.py` (v2 merged to main in PR #177, 2026-07-06) |
| D1 id still a placeholder | `grep -n "database_id" apps/web/wrangler.jsonc` |
| bunfig guard + excludes | `cat apps/web/bunfig.toml` |
| Flags still unwired / #121 still open | `gh issue view 121 --repo LaithAlz/stoop-backend --json state` and `grep -ri posthog apps/api/app --include='*.py'` (expect no hits) |
| `app_role` LOGIN flip status | Ask the operator / check startup logs for the role-separation WARNING — do NOT probe the live DB from an agent session |
| Scenario corpus still 20 (11 + 9) | `ls apps/api/evals/scenarios/*.yaml \| wc -l` and `ls apps/api/evals/scenarios/negative_prefilter \| wc -l` |
