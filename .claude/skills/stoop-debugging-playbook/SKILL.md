---
name: stoop-debugging-playbook
description: Symptom-to-triage playbook for known Stoop failure modes and shell traps. Load this when you hit any of - DuplicatePreparedStatementError / "prepared statement already exists" against Supabase; flaky or intermittent 401s in auth tests; dozens of integration tests ERRORing with "connection refused" to test:test@localhost:5432/test; mass asyncpg connect TimeoutErrors (wedged Docker); a bare `uv run pytest` collecting the paid eval test; pytest piped to tail hiding a failure; psycopg_pool.PoolClosed from the LangGraph checkpointer; the app.current_landlord_id GUC coming back empty mid-handler / RLS queries suddenly returning zero rows; Twilio webhook /sms returning 5xx; DeadlockDetectedError in test_downgrade_removes_table; pg_shdepend role-drop test failing; Docker daemon wedged; a *_double_timeout_shares_one_end_to_end_deadline test failing on elapsed-time assertions; eval-run failures (INCONCLUSIVE scenarios, ScenarioInfraError, 429/529 rate limits, judge verdict that contradicts its own reasoning). Also load before debugging ANY test failure in apps/api, to rule out the environment traps first.
---

# Stoop debugging playbook — symptom → triage

Repo root: `/Users/laith/Businesses/LandlordAI`. Backend: `apps/api`
(Python 3.12 / FastAPI / uv). Most failures below were hit, diagnosed, and
fixed during the founding sessions (2026-06 → 2026-07); git history is
forward-only, so the repo records the fixes but not the failures. This file
is the failure record. Check the table BEFORE deep-diving — most "new" bugs
here are one of these.

## When NOT to use this skill

| You actually want | Go to |
|---|---|
| The full incident history (root cause + evidence per incident) | `stoop-failure-archaeology` |
| Recreating the dev environment from scratch (uv, Docker, .env) | `stoop-build-and-env` |
| Running the app, migrations, live-DB discipline, operator flips | `stoop-run-and-operate` |
| What counts as evidence; test/eval discipline; running the paid gate | `stoop-validation-and-qa` |
| Config axes, defaults, boot gates (`APP_DATABASE_URL`, etc.) | `stoop-config-and-flags` |
| Design invariants you might be about to violate while "fixing" | `stoop-architecture-contract` |
| How a fix ships once you have it (gates, reviewers) | `stoop-change-control` |
| Measurement scripts and interpretation guides | `stoop-diagnostics-and-tooling` |

## Safety rails (read before running anything)

1. **Never run paid evals autonomously.** `uv run pytest -m eval` and
   `uv run python -m evals.runner` (without `EVAL_DRY_RUN=1`) hit the real
   Anthropic API (paid — current cost/duration figures live in
   `stoop-change-control` rule 9). Paid runs require the founder's
   go-ahead (session-verified 2026-07-05; no repo artifact).
2. **Never connect to live Supabase** (any `*.pooler.supabase.com` host)
   while debugging. Live dry-runs are an operator/orchestrator step — see
   `stoop-run-and-operate`.
3. **Never `source .env`, never `cat`/`echo` secrets.** A `source .env`
   under zsh once echoed the live Twilio token into the terminal and forced
   a rotation (session-verified 2026-07-05; no repo artifact).
4. Debugging never justifies bypassing change control — fixes still go
   through the `/ship` flow (`stoop-change-control`).

## The safe pytest invocation (memorize this)

Agent shells reset `cwd` between tool calls and env vars do not survive
across calls (session-verified 2026-07-05; no repo artifact). So every test
run is ONE self-contained command — explicit `cd`, explicit `DATABASE_URL`,
eval marker excluded, exit code printed:

```bash
cd /Users/laith/Businesses/LandlordAI/apps/api && \
  DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop' \
  uv run pytest -m "not eval" -q; echo EXIT=$?
```

- The creds match repo-root `docker-compose.yml` (`POSTGRES_DB/USER/PASSWORD`
  all `stoop`).
- Since PR #177 (2026-07-06), `pyproject.toml` carries
  `addopts = "-m 'not eval'"`, so even bare `uv run pytest` excludes the
  paid `@pytest.mark.eval` gate in `tests/test_evals.py`; keeping the
  explicit `-m "not eval"` is harmless belt-and-suspenders. The deliberate
  gate invocation is an explicit `-m eval` (CLI overrides addopts).
- `; echo EXIT=$?` is mandatory: `uv run pytest ... | tail` returns *tail's*
  exit status, which repeatedly masked real failures as passes
  (session-verified 2026-07-05; no repo artifact).

## The triage table

Terms used once: **Supavisor** = Supabase's connection pooler (port 6543 =
transaction mode, swaps physical Postgres backends between transactions).
**GUC** = a Postgres configuration variable, here `app.current_landlord_id`,
which RLS (row-level security) policies key off. **JWKS** = the JSON Web Key
Set the auth verifier fetches from Supabase. **respx** = the httpx mocking
library used in auth tests. **Tier-0** = the deterministic regex emergency
prefilter (`app/agent/prefilter.py`).

| Symptom | First check | Root cause | Fix | Artifact |
|---|---|---|---|---|
| Intermittent `asyncpg.exceptions.DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_N__" already exists`, only against Supabase port 6543 | Are all THREE knobs present in `_ASYNCPG_POOLER_CONNECT_ARGS`? | Supavisor transaction pooling multiplexes backends; asyncpg's statement cache assumes a stable session. The documented 2-knob SQLAlchemy recipe is INSUFFICIENT: `pool_pre_ping`'s ping calls asyncpg's raw `fetchrow(";")`, bypassing the dialect's `_prepare()` and using asyncpg's OWN driver-level cache with its own sequential names | Three knobs, all mandatory, never remove any: `prepared_statement_cache_size=0` (dialect cache) + `prepared_statement_name_func` (uuid4 names) + `statement_cache_size=0` (asyncpg driver cache — the third knob the ping needs) | `app/db/session.py::_ASYNCPG_POOLER_CONNECT_ARGS`; same block in `migrations/env.py`. Live probe: 18/100 failures with 2 knobs → 0/100 with 3 (session-verified 2026-07-05; no repo artifact) |
| Flaky, order-dependent 401s in auth tests (pass alone, fail in suite, or only on slow CI) | Does the test (or a helper it calls) open a **nested** `respx.mock` context? | Five issues' worth of causes (#141/#145/#147/#157/#158): nested respx contexts; JWK P-256 coordinates not zero-padded to the full 32-byte width (RFC 7518 §6.2.1 — a leading-zero coordinate then fails verification); the JWKS cache's `asyncio.Lock` surviving across per-test event loops (`asyncio_default_fixture_loop_scope = "function"` → one loop PER TEST → `RuntimeError: ... attached to a different loop`, caught and surfaced as 401); stale forced-refresh/degenerate/exception cooldown stamps leaking between tests | The conftest autouse fixture `_reset_jwks_auth_state` calls `_JwksState.reset_for_tests()` before EVERY test — it resets cache + all three cooldown stamps AND replaces the Lock. Rules: never nest respx contexts; always pad EC coordinates to fixed curve width | `app/integrations/supabase_auth.py::_JwksState`; `tests/conftest.py::_reset_jwks_auth_state`; `tests/test_auth.py` (respx-nesting NOTE ~line 734; leading-zero regression "Test 23" ~line 1003) |
| Dozens of integration tests ERROR: connection refused to `postgresql+asyncpg://test:test@localhost:5432/test` | Did THIS command export `DATABASE_URL`? (it does not survive from a previous tool call) | `tests/conftest.py::_PLACEHOLDER_ENV` sets a fake `DATABASE_URL` via `os.environ.setdefault` so `app.config` can import without real creds; with no export, integration tests connect to that nonexistent placeholder DB. Produced repeated "204 errors" / "37 errors" false alarms (session-verified 2026-07-05; no repo artifact) | Use the safe invocation above — `cd` + `DATABASE_URL` export + pytest in ONE shell command | `tests/conftest.py::_PLACEHOLDER_ENV` |
| Mass integration ERRORs: asyncpg connect `TimeoutError` against the CORRECT `stoop:stoop@localhost:5432/stoop` URL (NOT the test:test placeholder — 44 such "failures" in one burst) | `docker ps` — does the CLI even reach the daemon? | Docker Desktop daemon wedged (three occurrences as of 2026-07-06; the third presented exactly this way and looked like mass DB breakage) | `pkill -f Docker; open -a Docker`, wait for the daemon, `cd /Users/laith/Businesses/LandlordAI && docker compose up -d`, re-run | recovery recipe (session-verified; no repo artifact) — same wedge as the compose-hang row below |
| Bare `uv run pytest` about to run (or running) a REAL Anthropic API call | `grep -c addopts apps/api/pyproject.toml` → `0` | No `addopts` marker guard exists (as of 2026-07-05); the paid `@pytest.mark.eval` gate at the bottom of `tests/test_evals.py` gets collected | Always `-m "not eval"`. Paid runs are founder-gated (session-verified 2026-07-05; no repo artifact) | `pyproject.toml` `[tool.pytest.ini_options] markers`; `tests/test_evals.py` ("THE PAID GATE" banner ~line 1564) |
| pytest "looked green" but something is clearly broken | Was the run piped (`\| tail`, `\| head`)? | The pipeline's exit status is the last command's — tail's 0 masks pytest's 1 | Append `; echo EXIT=$?` to the pytest segment, every time | (session-verified 2026-07-05; no repo artifact) |
| "No such file or directory", relative paths failing mid-session | `pwd` | Agent-tool shells reset cwd between calls | `cd` explicitly in every invocation; prefer absolute paths | (session-verified 2026-07-05; no repo artifact) |
| `docker compose up -d` hangs; docker CLI can't reach the daemon | `docker ps` | Docker Desktop daemon wedged (three occurrences as of 2026-07-06 — the third surfaced as the asyncpg-TimeoutError burst row above) | `pkill -f Docker; open -a Docker`, wait for the daemon, then `cd /Users/laith/Businesses/LandlordAI && docker compose up -d` | repo-root `docker-compose.yml` (the compose file); recovery recipe (session-verified; no repo artifact) |
| `test_downgrade_to_0004_removes_role_policies_and_grants` fails — `app_role` still exists after downgrade | List databases on the local cluster; look for strays (e.g. `stoop_review`) | Postgres roles are CLUSTER-global. The 0005 downgrade's `pg_shdepend` guard sees a sibling database's grants still referencing `app_role` and — by design — leaves the role in place instead of failing the downgrade; the test then finds leftover state | Drop stray databases created by review agents/sessions, re-run | `migrations/versions/0005_app_role_and_rls.py` (DOWNGRADE + `pg_shdepend` guard docstring); `tests/test_migrations_0005.py::test_downgrade_to_0004_removes_role_policies_and_grants`; the `stoop_review` incident (session-verified 2026-07-05; no repo artifact) |
| `tests/test_migrations.py::test_downgrade_removes_table` hits `DeadlockDetectedError` dropping RLS policies | Re-run that single test in isolation — it passes | **Known OPEN flake #174** (as of 2026-07-05): lock contention between the 0005 policy DROPs and another session's catalog access on the shared local DB (three test modules each cycle migrations) | Re-run; do NOT "fix" blindly. Candidate fixes live in the issue (dedicated template DB per module, or an advisory lock) — both unbuilt | `gh issue view 174 --repo LaithAlz/stoop-backend` |
| A `*_double_timeout_shares_one_end_to_end_deadline` test fails on its wall-clock assertion (`assert elapsed < 0.6`) | Re-run that single test standalone with the safe invocation — does it pass alone? | **Load-sensitive flake, not a deadline bug** (noted in the #187/#188 review rounds, 2026-07-12): these tests shrink `CLASSIFICATION_BUDGET_SECONDS` to an artificial 0.3 s and assert total elapsed < 0.6 s — a loaded machine (parallel agents, Docker churn, full-suite contention) eats the margin | Re-run standalone before treating as real; only a reproducible standalone failure is evidence against the shared-deadline logic in `app/integrations/anthropic.py` | the deadline-sharing tests (three as of 2026-07-12) in `tests/test_agent_classify_intent.py`, `tests/test_agent_classify_severity.py`, `tests/test_agent_draft_response.py` |
| RLS-scoped handler suddenly gets ZERO rows mid-handler — no error, GUC `app.current_landlord_id` reads empty | Search the handler for `await session.commit()` | The GUC is set via `set_config(..., is_local=true)` = `SET LOCAL` = **transaction**-scoped. A mid-handler commit ends the transaction AND the GUC; every later query runs unscoped and RLS fails closed to zero rows — silent, not an error | Never call `session.commit()` inside a handler that used `require_landlord`; `get_session`'s teardown commit is the only commit | `app/deps.py::require_landlord` (the "WARNING for #53-57 authors" paragraph) |
| `/webhooks/twilio/sms` returns 5xx — someone wants to "fix" it to 200 | Read the transaction-design docstring before touching anything | **BY DESIGN.** The message INSERT commits FIRST in its own transaction; if the conflict-path recovery afterwards fails, 5xx tells Twilio to retry — **retry IS the recovery mechanism**. An earlier 200-here revision silently lost messages. `/status` is the mirror image: always 200 once the signature is valid (its whole body is one try/except), because Twilio retry-storms on any non-2xx | Do not change either endpoint's status behavior | `app/routers/webhooks/twilio.py` module docstring (four-part design; `_safe_step` / `_isolated_session`) |
| `psycopg_pool.PoolClosed: ... is not open yet` from the LangGraph checkpointer | Which entrypoint ran the graph? | Ordering contract, not a DB outage: `setup_checkpointer()` opens the pool; the API lifespan calls it, but a script/worker/test that uses `get_checkpointer()` first never opened it | Call `setup_checkpointer()` before first use in any non-API entrypoint | `app/agent/checkpointer.py::get_checkpointer` docstring ("ORDERING CONTRACT") |
| Eval scenario shows `ERROR (inconclusive -- re-run)` | Read the run summary's `errored` list vs its failed lists | `ScenarioInfraError` (backoff exhausted, harness crash) = INCONCLUSIVE — an infra failure, never a rubric miss. It still sets `release_blocked` (unproven ≠ proven-good) | Re-run the scenario (paid → founder-gated). Do not touch prompts/rubric for infra errors | `evals/runner.py::ScenarioInfraError` + `evals/scoring.py` (merged to main in PR #177 (squash 3ddd15e, 2026-07-06)) |
| Eval run crawling, or dying on 429 `RateLimitError` / 529 `OverloadedError` | Check the pacing knobs | Tier-1 Anthropic key rate limits; the runner paces itself. Retryable ONLY when `exc.__cause__` is `anthropic.RateLimitError` or `anthropic.OverloadedError` — everything else fails the scenario as infra | Knobs (`evals/runner.py`): `EVAL_TOKEN_BUDGET_PER_MIN` (default 25000, 60s sliding window of actual input tokens), `EVAL_RATE_LIMIT_MAX_RETRIES` env (default 6), exponential backoff base 2s capped at `RATE_LIMIT_BACKOFF_CAP_SECONDS = 70.0` (a full 60s window refill must fit inside one step). Expected duration per full run: `stoop-change-control` rule 9 | `evals/runner.py` constants ~lines 350–370 (merged to main in PR #177 (squash 3ddd15e, 2026-07-06)) |
| LLM judge FAILS a draft that reads fine | **Cross-check `judge_reasoning` prose against the boolean checklist dicts in `evals/results/last-run.json`.** Prose says pass + booleans say fail = **eval-infra bug, not product bug** | Historical causes: checklist items quoted as bullet strings in the judge prompt → the model re-keyed them loosely; free-form dict keys mismatched the verbatim checklist keys → lookups silently defaulted to `False` (silent inversion) | Fixed in 3 layers: `_normalize_checklist_key` / `_lookup_checklist_item` tolerant matching; a DISTINCT "NO MATCHING KEY" failure (never silently False); unquoted numbered checklist in the judge prompt. If you see "NO MATCHING KEY" in failures, fix the harness — never the rubric or prompts | `evals/scoring.py` ~lines 186–265; `evals/judge.py` (merged to main in PR #177); triage rule itself (session-verified 2026-07-05) |
| Eval scenarios INCONCLUSIVE with Pydantic `extra_forbidden` / missing-field errors on tool output | Look at the raw tool output: is the real payload nested under ONE unknown wrapper key (e.g. `{"severity_result": {...}}`)? Or (gate-8 shapes) is `refusal_flags` a per-flag boolean dict, or is there an invented `vulnerable_occupant_modifier_applied` bool? | Tool-forced LLM outputs drift in shape — three variances so far: single-key wrapper; flag bool-dict; invented boolean modifier | `_unwrap_single_key_wrapper` model_validators on the agent schemas and `JudgeVerdict`; `31bd498` added `_coerce_flag_dict_to_list` + a boolean-modifier absorb; `2163bd4` (safety review 2026-07-06) made the absorb FAIL-CLOSED: `False` absorbed, `True` absorbed only when severity is already EMERGENCY (recorded as a modifier string) — `True` below EMERGENCY still raises, because `modifier` never re-derives severity and absorbing it would silently bypass the retry→`classification_failed`→landlord-notification path. Trap when adding a new coercion: Pydantic runs multiple `mode="before"` model validators in REVERSE definition order — compose into ONE validator with explicit sequencing and keep the composition test green (`test_severity_result_wrapper_plus_gate8_variances_compose`) | `app/agent/schemas.py::_unwrap_wrapper` (reverse-order hazard in its docstring); `evals/judge.py` (merged to main in PR #177 (squash 3ddd15e, 2026-07-06)); incident A22 in `stoop-failure-archaeology` |

## Discriminating experiments — env vs code vs platform

Run these to classify a failure before writing any fix.

1. **Environment vs code.** Re-run the single failing test, in isolation,
   with the full safe invocation (explicit `cd`, explicit `DATABASE_URL`,
   `; echo EXIT=$?`). Passes alone but fails in the suite → cross-test
   state. The known shape: a module-level singleton holding an asyncio
   primitive (Lock, pool) + one event loop per test. Check whether a
   conftest autouse reset covers it — three already exist
   (`_reset_jwks_auth_state`, `_reset_weather_cache`,
   `_reset_checkpointer_pool` in `tests/conftest.py`); a fourth singleton
   needs a fourth reset, not a re-ordering of tests.
2. **Flaky vs real.** Re-run 3× in isolation. `DeadlockDetectedError` in
   `test_downgrade_removes_table` that clears on re-run is #174 — record it,
   don't fix it inline.
3. **Code vs platform.** Local Docker Postgres runs you as bootstrap
   superuser and is **blind to privilege bugs**; live Supabase differs. The
   three live-probed privilege traps (postgres-not-superuser, the
   `pg_has_role` MEMBER trap, the `GRANT … TO CURRENT_USER` connection kill)
   are cataloged in `stoop-domain-reference` §Supabase pack and the
   0004/0005 migration docstrings ("LIVE ROLE FACTS"). Operational
   consequence: a migration touching roles/grants/RLS that is green locally
   is NOT proven — it requires a live Supabase dry-run before merge
   (standing rule; the dry-run is operator-gated, see
   `stoop-run-and-operate`). Do not connect to the live pooler yourself.
4. **Eval-infra vs product.** Exercise the whole harness for free with
   `cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner` (the
   dry-run seam substitutes a fake tool-caller; zero API calls). Then apply
   the two table rules: INCONCLUSIVE = re-run, and judge-prose-vs-booleans
   disagreement = harness bug. Only a reproducible semantic failure on a
   real run is evidence against prompts/rubric — and rubric changes have
   their own process (`stoop-change-control`).

## Provenance and maintenance

Drift-prone claims and a one-line re-verification for each (all from
`/Users/laith/Businesses/LandlordAI` unless noted):

| Claim | Re-verify with |
|---|---|
| Three pooler knobs present in both engines | `grep -n "statement_cache_size" apps/api/app/db/session.py apps/api/migrations/env.py` |
| Conftest placeholder URL is `test:test@localhost:5432/test` | `grep -n "test:test@localhost" apps/api/tests/conftest.py` |
| addopts guard present (bare pytest excludes eval) | `grep -n addopts apps/api/pyproject.toml` (expect `-m 'not eval'`) |
| Local Docker creds are `stoop:stoop@localhost:5432/stoop` | `grep -n "POSTGRES_" docker-compose.yml` |
| Three autouse resets still exist | `grep -n "autouse" apps/api/tests/conftest.py` |
| #174 deadlock flake still open | `gh issue view 174 --repo LaithAlz/stoop-backend` |
| GUC is still `SET LOCAL`-scoped (`is_local=true`) | `grep -n "set_config" apps/api/app/deps.py` |
| Checkpointer ordering contract unchanged | `grep -n "PoolClosed" apps/api/app/agent/checkpointer.py` |
| Webhook 5xx-by-design contract unchanged | `grep -n "retry, which is exactly the recovery mechanism" apps/api/app/routers/webhooks/twilio.py` |
| Eval pacing knob names/defaults | `grep -n "EVAL_TOKEN_BUDGET_PER_MIN\|RATE_LIMIT_MAX_RETRIES\|RATE_LIMIT_BACKOFF_CAP_SECONDS" apps/api/evals/runner.py` |
| Judge key-matching layers present | `grep -n "_normalize_checklist_key\|NO MATCHING KEY" apps/api/evals/scoring.py` |
| respx-nesting rule + leading-zero JWK regression still in place | `grep -n "never nest respx\|leading zero" apps/api/tests/test_auth.py` |
| Eval cost/duration figures | one home: `stoop-change-control` rule 9; re-measure from the next founder-approved run's `last-run.json` cost fields |
