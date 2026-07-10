---
name: stoop-failure-archaeology
description: >
  The settled-battle chronicle for the Stoop repo — every incident and
  pre-merge catch from the founding sessions (2026-06-13 → 2026-07-06):
  symptom, root cause, fix, status, surviving artifact. Load BEFORE
  re-opening any past design decision, and on historical-smelling symptoms:
  DuplicatePreparedStatementError on the Supabase pooler; "must be able to
  SET ROLE"; a webhook 200 while the message row vanished; duplicate
  emergency_call notifications; flaky 401s in auth tests; API 400
  "temperature is deprecated"; Pydantic extra_forbidden on wrapper-keyed
  tool output; a judge whose prose says PASS but booleans say FAIL; a Tier-0
  miss on "smelled like gas"; a draft suggesting the oven for warmth;
  refusal text tripping guards; leaked secrets; premature merges. Also load
  before proposing to remove the asyncpg pooler knobs, re-add temperature,
  switch RLS to FORCE, grant roles to postgres, or edit prompts/v1.py or
  v2.py — settled battles. Owns the incident record only; siblings own live
  triage, gates, invariants.
---

# Stoop failure archaeology — the settled-battle chronicle

Git history in this repo is clean and forward-only: every incident below was caught
**before** merge, so the repo records the fixes but not the failures. This file is the
failure record. Its job is to stop you from re-fighting a battle that already produced
a ruling, and to tell you exactly which artifact enforces each ruling today.

Covers the founding engineering sessions, 2026-06-13 → 2026-07-06 (~40 merged PRs,
#123–#177, on `LaithAlz/stoop-backend`; the `test/eval-harness` rounds merged as PR #177 on 2026-07-06).

**Attribution convention used throughout:** facts with a surviving repo artifact cite
it (file / test / migration / PR). Facts that exist only in session memory are marked
"(session-verified 2026-07-05; no repo artifact)" — treat those as true but
re-verifiable only by reproduction, not by reading the repo.

**Branch caveat (as of 2026-07-06):** incidents A8–A13, A19, A21, and A22 name
artifacts that live on branch `test/eval-harness` (= `main` + commits `f38d0f0`,
`1d2c793`, `e9ce472`, `33441d2`, `11564c8`, `31bd498`, `7fe8609`). The eval PR is
now merged as PR #177 (2026-07-06). Verify those directly on `main`
until it merges.

## When NOT to use this skill

| You are trying to… | Use instead |
|---|---|
| Triage a **new** symptom right now (step-by-step diagnosis, shell traps as runbook) | `stoop-debugging-playbook` |
| Ship a change (gates, reviewer matrix, never-break rules as process) | `stoop-change-control` |
| Understand the design invariants themselves (why the architecture is shaped this way) | `stoop-architecture-contract` |
| Look up Supabase platform behavior, rubric doctrine, SMS/LLM-safety theory | `stoop-domain-reference` |
| Run tests/evals or judge what counts as evidence | `stoop-validation-and-qa` |
| Set up the dev environment or fix an env trap | `stoop-build-and-env` |
| Operate the live system (migrations, operator flips) | `stoop-run-and-operate` |

This skill answers "has this happened before, and what was decided?" — nothing else.

## Settled battles — do NOT re-litigate

If a proposal touches one of these, the answer is already NO. Cite the incident and
artifact instead of re-arguing.

| # | Settled ruling | Incident | Enforcing / recording artifact |
|---|---|---|---|
| 1 | RLS is `ENABLE ROW LEVEL SECURITY`, **never** `FORCE` — FORCE broke first-login provisioning | A3 | `apps/api/migrations/versions/0005_app_role_and_rls.py` ("WHY NO FORCE" docstring) |
| 2 | The **three** asyncpg pooler knobs (`prepared_statement_cache_size=0`, `prepared_statement_name_func`, `statement_cache_size=0`) are all load-bearing — never remove any | A1 | `apps/api/app/db/session.py::_ASYNCPG_POOLER_CONNECT_ARGS`; duplicated in `apps/api/migrations/env.py` |
| 3 | Deferral/refusal policy text is appended **by code**, verbatim from `REFUSAL_TEMPLATES` — the model acknowledges only, never weaves/paraphrases policy | A12, A13, A21 | `apps/api/app/agent/nodes/draft_response.py::_append_deferrals` + `app/agent/prompts/v2.py::REFUSAL_TEMPLATES` (the live source as of 2026-07-06; `prompts/v1.py` is frozen history) |
| 4 | `temperature` is **omitted** on `claude-sonnet-5` (API 400s on it) — never re-add it; determinism is the eval gate's job | A7 | `apps/api/app/integrations/anthropic.py` (comment at the `messages.create` call); `tests/test_integrations_anthropic.py` asserts `"temperature" not in call_kwargs` |
| 5 | Webhook persistence is **commit-first** — message INSERT commits in its own transaction before any processing; never one shared transaction | A4 | `apps/api/app/routers/webhooks/twilio.py` module docstring (transaction design point 1) |
| 6 | **No custom-role membership grants involving `postgres`** in any migration — use a postgres-owned `SECURITY DEFINER` function with pinned `search_path` | A2 | `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py` |
| 7 | Append-only means **insert-time-only**: `messages.classification`/`tokens_*`/`model`/`llm_cost_cents` are DEPRECATED (never written); the canonical classification record is the `audit_log` `'classified'` row | schema v1.6 ruling | `docs/03-engineering/schema-v1.md` v1.6 amendment + per-column DEPRECATED comments |
| 8 | `app/agent/prompts/v1.py` AND `v2.py` are **frozen** — never edit an existing prompt version; add `v{n+1}` (v3 next, as of 2026-07-06) + full eval run | CLAUDE.md rule | `apps/api/CLAUDE.md` (Agent rules); v2's own docstring ("FROZEN once merged; to change behavior add v3") |
| 9 | Tier-0 prefilter changes are **monotonic-additive** (no HARD→silent flips) and every change ships a regression test class | A10, PR #144 | `apps/api/tests/test_prefilter.py` (18 `TestRegression*` classes, 41 test classes total, as of 2026-07-06 — recount: `grep -c "class TestRegression"`) |

Definitions used above: **RLS** = Postgres Row-Level Security. **Tier-0 prefilter** =
the deterministic, no-I/O emergency keyword filter (`app/agent/prefilter.py`) that
runs in the webhook before the LLM graph; it may only be escalated past, never
de-escalated (see `apps/api/CLAUDE.md`).

## The chronicle — incidents A1–A22

### A1. Supavisor DuplicatePreparedStatementError (PR #165)
- **Symptom:** intermittent `DuplicatePreparedStatementError` / `prepared statement "__asyncpg_stmt_xx__" already exists` against the Supabase transaction pooler (Supavisor, port 6543).
- **Root cause:** transaction pooling multiplexes many client sessions onto shared server backends; asyncpg's prepared-statement cache assumes a stable session. SQLAlchemy's documented 2-knob recipe (`prepared_statement_cache_size=0` + UUID name func) was insufficient because `pool_pre_ping`'s ping bypasses the dialect layer and uses asyncpg's *own* cache — a third, asyncpg-level knob `statement_cache_size=0` is required.
- **Evidence:** live probe loop, 100 requests: 18/100 failures with 2 knobs → 0/100 with 3 (session-verified 2026-07-05; no repo artifact). The rationale survives in the long comment block in `apps/api/app/db/session.py`.
- **Fix:** all three knobs in `apps/api/app/db/session.py::_ASYNCPG_POOLER_CONNECT_ARGS`, mirrored in `apps/api/migrations/env.py` so Alembic gets the same treatment.
- **Status:** FIXED — PR #165 "fix(api): asyncpg prepared-statement compat for Supabase transaction pooler" [MERGED].
- **Surviving artifact:** `apps/api/app/db/session.py` lines defining `_ASYNCPG_POOLER_CONNECT_ARGS`; `apps/api/migrations/env.py` same-named dict. Never remove any of the three.

### A2. Role-migration privilege ordering — the live-dry-run lesson (issue #15)
- **Symptom:** migration 0004 (`auth.users` trigger) was green on local Docker Postgres but FAILED on live Supabase with `must be able to SET ROLE` (the original design used a dedicated trigger-owner role).
- **Root cause:** three live-probed Supabase platform facts (session-verified 2026-07-05; no repo artifact — the theory lives in `stoop-domain-reference`): (1) `postgres` on Supabase is NOT superuser (it is `rolbypassrls=TRUE`, as is `service_role`); (2) `pg_has_role(current_user, 'newrole', 'MEMBER')` returns TRUE immediately after `CREATE ROLE` on PG16+ (implicit ADMIN OPTION) — unsound as a membership/idempotency guard; (3) `GRANT <role> TO CURRENT_USER` executed as `postgres` TERMINATES the connection, on both pooler ports 5432 and 6543.
- **Fix:** redesigned to a postgres-owned `SECURITY DEFINER` function set with `SET search_path = public, pg_temp` pinned on all functions; NO custom-role membership grants involving postgres anywhere.
- **Status:** FIXED — migration `0004_auth_users_lifecycle_trigger.py`, PR #166 "feat(api): auth.users lifecycle trigger — provision, email sync, soft-delete" [MERGED].
- **Surviving artifact:** `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py` (docstring records the redesign). **Standing rule** (see `stoop-change-control`): any migration touching roles/grants/RLS gets a live Supabase dry-run before merge — local Docker runs as bootstrap superuser and is blind to privilege bugs.

### A3. FORCE RLS front-door outage (#22 design phase)
- **Symptom:** with `FORCE ROW LEVEL SECURITY`, `/v1/me` first-login provisioning died — the landlord INSERT happens BEFORE any landlord identity exists to set in the session GUC (Postgres run-time configuration variable used by the RLS policies).
- **Root cause:** FORCE binds even the table owner to RLS, so the pre-identity provisioning path has no lawful writer (session-verified reproduction 2026-07-05; the full rationale survives in the migration docstring).
- **Fix:** RLS is `ENABLE` (not `FORCE`) + provisioning runs on the admin engine via `get_admin_session` — the deliberately-unscoped escape hatch, locked down by an allowlist test.
- **Status:** FIXED — migration `0005_app_role_and_rls.py` design, PR #167 "feat(api): RLS on every table + app_role + append-only REVOKEs" [MERGED].
- **Surviving artifact:** `apps/api/migrations/versions/0005_app_role_and_rls.py` ("WHY NO FORCE" docstring section); `apps/api/tests/test_migrations_0005.py::test_get_admin_session_referenced_only_by_allowlisted_files`. Do not "upgrade" ENABLE to FORCE without re-solving provisioning.

### A4. Silent message loss in the Twilio webhook (#40)
- **Symptom:** Twilio POST returned 200 but the message row VANISHED. (Twilio never retries a 200 — the message was gone forever.)
- **Root cause:** a `_safe_step` helper swallowed an exception inside the ONE shared transaction → transaction aborted → the message INSERT rolled back while the handler still returned 200.
- **Evidence:** reproduced independently by both adversarial reviewers pre-merge (session-verified 2026-07-05; no repo artifact).
- **Fix:** COMMIT-FIRST persistence — persist + commit the message in its own transaction immediately, before any processing; recovery/artifact failures return 5xx, because Twilio's retry IS the recovery mechanism.
- **Status:** FIXED — PR #171 "feat(api): Twilio inbound webhook + status callback — the front door" [MERGED].
- **Surviving artifact:** `apps/api/app/routers/webhooks/twilio.py` — the module docstring states the contract ("The message INSERT commits in its own transaction immediately").

### A5. Duplicate emergency escalations under concurrency (#40)
- **Symptom:** concurrent replay of the same Twilio webhook produced duplicate `emergency_call` notifications — 3/3 dupes in a 3-way race, 28/30 across a burst (session-verified 2026-07-05; no repo artifact).
- **Root cause:** check-then-insert on notifications had no unique constraint backing it.
- **Fix:** partial unique index `uq_notifications_message_dedupe` on `(payload->>'message_id', type) WHERE type IN ('emergency_call', 'needs_eyes')`, paired with `INSERT … ON CONFLICT DO NOTHING RETURNING id` in the webhook handler; alert side-effects fire only when the RETURNING row came back. `emergency_call`/`needs_eyes` rows are NEVER deleted (they are the durable dedupe record).
- **Status:** FIXED — PR #171 + migration 0006.
- **Surviving artifact:** `apps/api/migrations/versions/0006_notifications_message_dedupe_index.py`; the `ON CONFLICT DO NOTHING RETURNING id` block in `apps/api/app/routers/webhooks/twilio.py`.

### A6. Case-sweep TOCTOU (#110)
- **Symptom:** a tenant contradiction ("actually it's still broken") arriving between the resolution sweeper's SELECT and its UPDATE was overwritten — the case auto-resolved anyway. (TOCTOU = time-of-check-to-time-of-use race.)
- **Root cause:** the sweep decided from a stale read instead of re-checking inside the write (session-verified reproduction 2026-07-05).
- **Fix:** self-guarding UPDATEs — the WHERE clause re-checks `pending_resolved_at` / status / `last_activity_at` inside the UPDATE itself; audit and `needs_eyes` writes are gated on `rowcount == 1` (a lost race, `rowcount == 0`, is a deliberate silent no-op). `awaiting_approval` cases are excluded from auto-stale sweeping entirely.
- **Status:** FIXED — PR #173 "feat(agent): deterministic nodes — property/context/case lifecycle + weather" [MERGED].
- **Surviving artifact:** `apps/api/app/agent/case_lifecycle.py` (`rowcount == 1` gating; `OPEN_STATUSES` minus `awaiting_approval` sweep set).

### A7. `temperature` deprecated on claude-sonnet-5
- **Symptom:** live smoke test returned API 400 when passing `temperature=0` ("`temperature` is deprecated for this model").
- **Fix:** the parameter is OMITTED entirely. Determinism is owned by the eval gate (3 classification samples, flaky = fail), not by a sampling knob. Also from the same senior review: `max_retries=0` on the SDK client — the product owns its retry budget, not the SDK.
- **Status:** FIXED — PR #175 "feat(agent): LLM nodes — intent, severity (rubric v1), drafting with hard guards" [MERGED].
- **Surviving artifact:** `apps/api/app/integrations/anthropic.py` (comment at the `messages.create` call + `max_retries=0`); `apps/api/tests/test_integrations_anthropic.py` asserts `"temperature" not in call_kwargs`. Do not "helpfully" re-add temperature.

### A8. LLM output wrapper-key nesting (eval gates 1–5)
- **Symptom:** tool-forced outputs arrived as `{"severity_result": {...}}` or `{"severity_input": {...}}` — one unknown key wrapping the real payload → Pydantic `extra_forbidden` + missing-field errors → scenarios INCONCLUSIVE.
- **Fix:** `_unwrap_single_key_wrapper` model_validators on `SeverityResult`/`IntentResult`/`DraftResult` and on the eval judge's `JudgeVerdict`: a dict with exactly one unknown key whose value is a dict containing known fields gets unwrapped; nonsense single-key dicts still fail validation.
- **Status:** FIXED — commit `1d2c793` on `test/eval-harness`, merged to main in PR #177 (squash 3ddd15e, 2026-07-06).
- **Surviving artifact:** `apps/api/app/agent/schemas.py::_unwrap_single_key_wrapper` (reused by `apps/api/evals/judge.py`); tests in `apps/api/tests/test_agent_schemas.py`.

### A9. Judge verdict inversion (eval gate 5)
- **Symptom:** the LLM judge's prose reasoning said the draft PASSED, but its boolean checklist dict said FAIL → scenarios hard-failed on drafts the judge itself called good.
- **Root cause:** (1) checklist items were quoted as bullet strings in the judge prompt, so the model re-keyed them loosely; (2) free-form dict keys mismatched the verbatim checklist keys → lookups defaulted to False — a silent inversion.
- **Fix (3 layers):** tolerant `_lookup_checklist_item` / `_normalize_checklist_key` (quote/whitespace/case normalization); a DISTINCT "NO MATCHING KEY" failure that can never silently read as False; an unquoted **numbered** checklist in the judge prompt with keys copied character-for-character; plus the A8 wrapper-unwrap on `JudgeVerdict`.
- **Status:** FIXED — commit `e9ce472` on `test/eval-harness`, merged to main in PR #177 (squash 3ddd15e, 2026-07-06).
- **Surviving artifact:** `apps/api/evals/scoring.py` (`_normalize_checklist_key`, `_lookup_checklist_item`, "NO MATCHING KEY"); `apps/api/evals/judge.py` (numbered-checklist prompt). **Triage rule this bought:** when a judge fails a draft, ALWAYS cross-check the judge's prose reasoning against the booleans in `evals/results/last-run.json` — disagreement means eval-infra bug, not product bug (see `stoop-debugging-playbook`).

### A10. E2 "smelled like gas" Tier-0 miss
- **Symptom:** eval scenario e2 (`prefilter_must_fire: true`) failed — "the kitchen has smelled like gas" did not trip Tier-0; the pattern had "smell"/"smells" but no past-tense inflections.
- **Fix:** a verb-tense-completion sweep across the trigger tables (smell/smells/smelled/smelt/smelling; flood tenses; overdos(e|ed|ing); collaps(e|es|ed|ing); sparks + sparked), done under the PR #144 discipline — monotonic ADDITIVE only, zero HARD→silent flips, every change gets a regression test class.
- **Status:** FIXED — commit `f38d0f0` on `test/eval-harness`, merged to main in PR #177 (squash 3ddd15e, 2026-07-06). Open follow-up: issue #176, the precision pass on the false-positive surface.
- **Surviving artifact:** `apps/api/app/agent/prefilter.py` (docstring narrates the E2 gap); `apps/api/tests/test_prefilter.py` regression classes; `apps/api/evals/scenarios/e2_gas_smell.yaml`.

### A11. Oven-for-warmth hazard in a draft (eval gate 5, e3)
- **Symptom:** the drafted reply to a no-heat emergency suggested using the oven for warmth — a carbon-monoxide/fire hazard.
- **Caught by:** the LLM judge; no hard guard existed for this class yet.
- **Fix:** `_UNSAFE_HEAT_SOURCE_RE` hard guard (life-safety class: oven/stovetop/open-flame near warmth words → reject + regenerate) plus `_UNSAFE_HEAT_SOURCE_GUIDANCE` injected on heating topics for EMERGENCY and URGENT severities.
- **Status:** FIXED — commit `e9ce472` on `test/eval-harness`, merged to main in PR #177 (squash 3ddd15e, 2026-07-06).
- **Surviving artifact:** `apps/api/app/agent/nodes/draft_response.py` (`_UNSAFE_HEAT_SOURCE_RE`, `_UNSAFE_HEAT_SOURCE_GUIDANCE`); tests in `apps/api/tests/test_agent_draft_response.py`.

### A12. Guard/deferral self-collision → code-appends architecture
- **Symptom:** when the model was asked to weave refusal-deferral language into its reply, its own paraphrase ("about the rent discount…") tripped the compensation guard → safe replies degraded to the generic fallback.
- **Root cause:** asking the model to restate policy text guarantees paraphrase drift, and the guards (correctly) can't tell drift from violation.
- **Fix:** SEPARATION — the model writes only a short acknowledgment and is told not to touch the topic; the CODE appends `REFUSAL_TEMPLATES` verbatim (`_append_deferrals`); `_strip_mandated_templates` scrubs template text before guard-checking as defense-in-depth. Senior-review ruling 2026-07-05: the model acknowledges only, never paraphrases policy (session-verified 2026-07-05; the ruling's mechanics are in the merged code).
- **Status:** FIXED — PR #175 [MERGED], refined in the `test/eval-harness` rounds.
- **Surviving artifact:** `apps/api/app/agent/nodes/draft_response.py` (`_append_deferrals`, `_strip_mandated_templates`, module docstring); `REFUSAL_TEMPLATES` — live source `app/agent/prompts/v2.py` as of 2026-07-06 (v1 = frozen history; see A21).

### A13. Refusal ack duplicated the appended hand-off (eval gate 6, f1)
- **Symptom:** scenario f1-rent-ltb judge fail, "plain-language-rules non-conformant": the draft read as two texts glued together — the model's ack said "passed this to the landlord, he'll follow up… talk soon", then the appended template said the hand-off AGAIN.
- **Root cause:** the instruction ITSELF mandated the duplication ("include ONE brief, neutral sentence noting you've passed it along…").
- **Fix:** refusal-ack instruction rewritten — the ack must NOT state the hand-off, NOT promise follow-up, NOT sign off (more text follows it).
- **Status:** CLOSED (2026-07-06). Ack fix committed (`33441d2`), but **gate 7 (18/20) still hard-failed f1** — the residual root cause was the v1 REFUSAL_TEMPLATES' own legalistic copy, not the ack. That produced the founder-approved templates-only **prompts v2** bump (commit `11564c8`) — see A21 for the rest of the chain, which **eval gate 9 verified: 20/20, `release_blocked=False` (2026-07-06)**. (The gate-6 u1 soft-fail was one-off draft nondeterminism.)
- **Surviving artifact:** `apps/api/app/agent/nodes/draft_response.py` (rewritten ack instruction, commit `33441d2`); tests in `apps/api/tests/test_agent_draft_response.py`.

### A14. Parallel-agent shared-tree destruction (session-only)
- **Symptom:** a frontend agent ran `git checkout` / `git stash` in the main working tree while another agent had UNCOMMITTED eval work there → work destroyed mid-flight (session-verified 2026-07-05; no repo artifact).
- **Recovery:** reflog + manual rework; a leftover stash was later verified subsumed and dropped.
- **Status:** STANDING RULE — one active agent per working tree; parallel repo work uses `git worktree add` (e.g. the web worktree `/Users/laith/Businesses/LandlordAI-web` for `feat/web-clarity-queue`). Process home: `stoop-change-control`.

### A15. Twilio auth token leaked into terminal output (session-only)
- **Symptom:** `source .env` under zsh errored and echoed the line CONTAINING the live Twilio auth token into the conversation/terminal log (session-verified 2026-07-05; no repo artifact).
- **Consequence:** token treated as compromised; the founder rotated it in the Twilio console.
- **Status:** STANDING RULES — NEVER `source .env`; parse it with Python (`shlex.quote`, print only export lines consumed by `eval "$(…)"`, never print values); never `cat`/`echo` secrets; `.env` is gitignored, never committed, never read by discovery agents. Runbook home: `stoop-build-and-env`.

### A16. Supabase project created in the wrong region (session-only)
- **Symptom:** the first live project landed in us-west-2; the docs require Canadian data residency (ca-central-1) (session-verified 2026-07-05; no repo artifact).
- **Fix:** project deleted and recreated in ca-central-1. Current project ref `kytqtdqmzwyhiwkafcbh` (as of 2026-07-05).
- **Status:** FIXED. Check region FIRST when creating any regional resource.

### A17. Merged before CI finished (session-only)
- **Symptom:** PR #166 was squash-merged while the FINAL commit's CI run was still pending — an earlier commit's green was mistaken for the tip's (session-verified 2026-07-05; no repo artifact).
- **Status:** STANDING RULE — before merge: `gh pr checks <N> --repo LaithAlz/stoop-backend --watch` AND verify the check run's `headSha` equals the branch tip. Process home: `stoop-change-control`.

### A18. Shell traps that produced false alarms (session-only, recurring)
All session-verified 2026-07-05; no repo artifact. Triage runbook home: `stoop-debugging-playbook`; listed here so the *incidents* are on record:
1. `uv run pytest … | tail` masks the exit code (the pipe returns tail's status) → always append `; echo EXIT=$?` or drop the pipe.
2. Missing `DATABASE_URL` export → integration tests hit the conftest PLACEHOLDER url (`postgresql+asyncpg://test:test@localhost:5432/test`) → dozens of connection ERRORs that look like real breakage ("204 errors", "37 errors" scares). Export `DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop'` IN THE SAME shell invocation as pytest — env does not survive across tool calls; `cd` explicitly every time.
3. Bare `uv run pytest` USED to collect the paid `@pytest.mark.eval` test — CLOSED by PR #177 (senior-review finding): `pyproject.toml` now has `addopts = "-m 'not eval'"`; explicit `-m eval` still runs the gate deliberately.
4. A leftover sibling database (`stoop_review`) made the pg_shdepend role-drop test fail — drop stray DBs created by review agents.
5. Docker Desktop daemon wedged — three occurrences as of 2026-07-06. The third presented as 44 integration-test "failures" that were pure asyncpg connect `TimeoutError`s against the CORRECT `stoop:stoop@localhost:5432/stoop` URL (not the placeholder-url shape in item 2). Recovery every time: `pkill -f Docker; open -a Docker`, wait, `docker compose up -d`.

### A19. Eval-gate infra hardening (gates 1–5)
- **Symptom:** 429 rate-limit bursts on a Tier-1 Anthropic account; and early crashes could lose the run record.
- **Fix:** token-budget pacing (`EVAL_TOKEN_BUDGET_PER_MIN`, default 25000, 60s sliding window fed by ACTUAL tokens_in) + exponential backoff capped at 70s, max 6 retries, retryable ONLY on RateLimitError/OverloadedError via `exc.__cause__`. Infra failure ≠ semantic failure: `ScenarioInfraError` → scenario INCONCLUSIVE (re-run), never a rubric miss — but it still sets `release_blocked`. `last-run.json` is ALWAYS written, even on crash paths.
- **Status:** BUILT — commit `f38d0f0` onward, on `test/eval-harness`, merged to main in PR #177 (squash 3ddd15e, 2026-07-06).
- **Surviving artifact:** `apps/api/evals/runner.py` (`EVAL_TOKEN_BUDGET_PER_MIN`, `ScenarioInfraError`, `LAST_RUN_PATH` → `evals/results/last-run.json`). Runs are paid and founder-gated; current cost/duration figures live in `stoop-change-control` rule 9. ONLY the orchestrator runs paid gates, with founder go-ahead.

### A20. Flaky 401s in auth tests (#141/#145/#147/#157/#158)
- **Symptom:** intermittent 401s across the auth test suite, five separate issues before the class was closed.
- **Root causes found:** nested respx contexts; JWK coordinate width (leading-zero P-256 coordinates); JWKS cache/lock state crossing event loops; missing rate-limit-stamp isolation between tests.
- **Fix:** consolidated `_JwksState` singleton (one lock, one cache) + the cooldown constants (`_FORCED_REFRESH_WINDOW_SECONDS`, `_DEGENERATE_FETCH_COOLDOWN_SECONDS`, `_FETCH_EXCEPTION_COOLDOWN_SECONDS`) in `app/integrations/supabase_auth.py`; conftest autouse fixtures call `reset_for_tests()` (JWKS state, weather cache, checkpointer pool) per test. **Testing rule: NEVER nest respx contexts.**
- **Status:** FIXED — see merged PR #141 ("test(api): globally reset JWKS cache+lock per test (fix flaky 401)"). Open cousin: issue #174 (below).
- **Surviving artifact:** `apps/api/app/integrations/supabase_auth.py::_JwksState` (+ `reset_for_tests`); `apps/api/tests/conftest.py` autouse resets.

### A21. v1 refusal-template copy failed the plain-language bar → prompts v2 (eval gates 7–8, f1)
- **Symptom:** eval gate 7 (18/20) hard-failed f1-rent-ltb AGAIN after the A13 ack fix — the judge's plain-language findings pointed at the appended template itself: v1's `legal_rent_ltb` was a 29-word legalistic sentence ("… anything related to the Landlord and Tenant Board on their behalf"), in text bound by `docs/02-product/plain-language-rules.md` (grade-5 reading level, ≤15-word sentences, never legalistic). `impersonation` had the same disease; `access_codes`/`cost_compensation` were stiff.
- **Root cause:** the template copy predated judge-enforced plain-language rules, and under the A12 code-appends architecture only a prompt version bump can change it.
- **Fix:** founder-approved (2026-07-06) **prompts v2** — a templates-only bump, commit `11564c8`: `legal_rent_ltb` + `impersonation` rewritten plain-language, `access_codes` + `cost_compensation` plained, `other_tenants` byte-identical; system-prompt builders re-exported from frozen v1 **by construction**; consumers (`classify_severity.py`, `draft_response.py`, `evals/runner.py`) import `prompts.v2`; drafts/audit rows stamp `prompt_version="v2"`.
- **Evidence/status:** CLOSED — verified by gate 9 (2026-07-06). Gate 8 — the first run under v2, `last-run.json` generated 06:26Z — went 18/20: f1 hard-failed once more on the ONE remaining relative-time word ("soon", banned by plain-language rule 4), and e4 was INFRA (malformed-output schema variance — see A22, inconclusive not semantic). Commit `31bd498` dropped "soon" from the v2 deferral (pre-merge amendment; v2 is frozen once MERGED) and absorbed the output variances. **Gate 9 (06:43Z record): 20/20, `release_blocked=False`** — the full gates-5→9 arc reads 14/20 → 19/20 → 18/20 (f1 = v1 template copy) → 18/20 (f1 = "soon"; e4 INFRA) → **20/20 GREEN**. The green baseline is committed as `apps/api/evals/results/v1-baseline.json` (commit `7fe8609`, root-`.gitignore` exception). Any LATER gate's verdict still lives only in `apps/api/evals/results/last-run.json`.
- **Surviving artifact:** `apps/api/app/agent/prompts/v2.py` (docstring narrates the failure); pin test `apps/api/tests/test_agent_schemas.py::test_prompts_v2_changes_exactly_the_founder_approved_templates`; commits `11564c8` + `31bd498` + baseline `7fe8609` on `test/eval-harness`.

### A22. Gate-8 e4 schema variances + the Pydantic reverse-order composition trap
- **Symptom:** eval gate 8's e4 (prompt-injection scenario) went INFRA-inconclusive on a THIRD model-output variance: `refusal_flags` returned as a per-flag boolean dict (`{"access_codes": false, …}`) instead of a list of fired flags, alongside an invented boolean field `vulnerable_occupant_modifier_applied`.
- **Root cause:** tool-forced outputs drift in shape (the A8 family); `extra="forbid"` correctly rejected the invented key, but rejecting = INCONCLUSIVE scenario = `release_blocked`.
- **Fix (commits `31bd498` then `2163bd4`, two deliberately-narrow coercions in `app/agent/schemas.py`):** `_coerce_flag_dict_to_list` (an exactly str→bool dict becomes the list of true keys); a boolean-modifier absorb that FAILS CLOSED (safety review 2026-07-06 caught the first version being fail-open): `False` is dropped (asserts nothing), `True` is absorbed only when severity is already EMERGENCY (recorded as a `modifier` string), and `True` below EMERGENCY still raises — `modifier` never re-derives severity, so absorbing it would have turned an injection-shaped `ROUTINE`+`true` payload from "validation error → retry → landlord notification" into a silent under-classification. Regression: `test_severity_result_boolean_modifier_true_below_emergency_fails_closed`.
- **The composition trap:** the absorb was first written as a second `mode="before"` model validator — and silently broke when the variances COMPOSED (wrapper key outside, invented key inside), because **Pydantic executes multiple `mode="before"` model validators in REVERSE definition order**. The fix folds unwrap-then-absorb into ONE validator (`_unwrap_wrapper`) with explicit sequencing. A composition test caught it: `tests/test_agent_schemas.py::test_severity_result_wrapper_plus_gate8_variances_compose`.
- **Status:** FIXED — commit `31bd498` on `test/eval-harness`; verified by gate 9 (e4 PASS, 20/20).
- **Surviving artifact:** `apps/api/app/agent/schemas.py` (`_unwrap_wrapper` docstring records the reverse-order hazard; class docstring narrates all three variances); composition tests in `apps/api/tests/test_agent_schemas.py`.

### A23. The #43 interrupt saga — a disproven "empirical" claim, a TOCTOU resume, and a reproduced stuck-draft window (2026-07-09/10)
- **Symptom(s), three defects that 1000+ green tests could not see:** (1) the shadow-mode branch shipped a `_drain_pending_interrupt_if_any` mechanism justified by a probe "proving" LangGraph replays a paused task with OLD values on plain re-invoke; (2) `resume_case_thread`'s staleness guard read-then-resumed across multiple awaits; (3) the crash-window completion-gate fix keyed on the case's AMBIENT status.
- **Root causes:** (1) **methodology error — the probe reused IDENTICAL inputs on both calls, which cannot distinguish replay-with-old-values from restart-with-new-values.** Spec-guardian's 10-line repro with distinguishable inputs (m1/m2) proved plain `ainvoke(new_state, config)` on an interrupt-paused thread RESTARTS from START on langgraph 1.2.7; the drain was dead code (all 9 tests passed with it commented out). (2) Classic TOCTOU: a landlord tap racing a tenant text could resolve the FRESH interrupt with an approval meant for the OLD draft; two concurrent resumes both passed the check (double-send once #44 wires send). (3) A case already `awaiting_approval` from message M1 made M2's crashed run look "complete" → every redelivery a silent no-op, D2 permanently unapprovable — REPRODUCED live by spec-guardian, the forbidden silent-dead-end class.
- **Fixes (PR #187, squash `a61b95e`):** drain removed + docstring records both the true semantics and the methodology error; per-case `pg_advisory_xact_lock` (xact-scoped = correct for Supavisor transaction pooling) across run_graph's case span AND the whole check→resume span, staleness re-read INSIDE the lock — proven by barrier-forced concurrent tests; completion gate keys on THIS message's thread checkpoint (`next` empty = terminal, live interrupt = paused; scheduled-but-never-executed = re-run), mutation-confirmed by reintroducing the old heuristic and watching the regression test fail.
- **LangGraph interrupt() operational contract (learned here, load-bearing):** the whole node re-executes on every resume attempt; NOTHING before a raising `interrupt()` commits (hence the mark/await two-node split); plain `ainvoke` with new input on a paused thread restarts from START, superseding the pending task. #44's send must be a SEPARATE node behind a conditional edge — never code after `interrupt()` (invariant pinned in `await_approval.py`'s docstring and on issue #44).
- **Status:** FIXED, merged 2026-07-10. Follow-ups tracked in #186 (pool-holding deadlock threshold ≥10 concurrent cases, idle-in-transaction timeout profiling, lock-key doc precision, langgraph ceiling pin — the semantics tests in CI are the regression guard).
- **Surviving artifacts:** `apps/api/app/agent/graph.py` (`_case_lock`, corrected module docstring), `apps/api/app/agent/nodes/await_approval.py`, `apps/api/app/agent/graph_entry.py` (`_thread_reached_terminal_or_paused_state`), `apps/api/tests/test_agent_shadow_interrupt.py` (15 tests incl. the concurrent and crash-window ones).

## Open wounds (as of 2026-07-06)

Not settled. Do not treat these as closed; do not silently "fix" them without the
normal gates (`stoop-change-control`).

| Wound | State | Where |
|---|---|---|
| Issue #174 — "flaky: test_downgrade_removes_table hits DeadlockDetectedError dropping RLS policies" | OPEN, flaky, unfixed (cousin of A20) | `gh issue view 174 --repo LaithAlz/stoop-backend` |
| Issue #176 — "prefilter precision pass: water active-flow + gas-leak FP surface (cry-wolf risk)" | OPEN — the recall sweep (A10) deliberately deferred precision | `gh issue view 176 --repo LaithAlz/stoop-backend` |
| ~~Eval-harness branch unmerged~~ RESOLVED: all 7 commits (A10 tense sweep, A11 heat-source guard, A13 ack fix, A21 prompts v2, A22 coercions, gate-9 baseline) merged to main in PR #177 (squash `3ddd15e`, 2026-07-06); gate 9 = 20/20 GREEN, `release_blocked=False` | `gh pr view 177 --repo LaithAlz/stoop-backend --json state,mergedAt`; verdict in `apps/api/evals/results/v1-baseline.json` |
| Issue #170 — "dead-letter table for unrouted inbound SMS (unknown To number)" | OPEN — unrouted inbound currently has no durable parking spot | `gh issue view 170 --repo LaithAlz/stoop-backend` |

## How to add to this chronicle

Every new production incident **and** every pre-merge catch that changed a design gets
an entry. The convention:

1. **Append an entry here** — next number (A23, A24, …), exact shape: Symptom →
   Root cause → Evidence → Fix → Status → Surviving artifact. Evidence must either
   name a repo artifact or carry the session-verified attribution with a date.
2. **Add an engineering-decisions entry** — the decision record lives at
   `docs/03-engineering/engineering-decisions.md` (as of 2026-07-05 still on the
   file on `main` since PR #179 (2026-07-06); append to the merged file
   once that PR lands). This file records *what happened*; that file records *what
   was decided and why*.
3. **Where classification was involved, add an eval scenario** — per
   `apps/api/CLAUDE.md`: "New production misclassification ⇒ new eval YAML in the
   same week" (`apps/api/evals/scenarios/`, format per
   `docs/02-product/eval-scenarios-v1.md`).
4. **Where Tier-0 was involved, add a regression test class** in
   `apps/api/tests/test_prefilter.py` (the #144 discipline — see settled battle 9).
5. If the incident produced a new never-break ruling, add a row to the
   settled-battles table above AND get it into `stoop-change-control`'s never-break
   list — the ruling's process home.

## Provenance and maintenance

Drift-prone claims and their one-line re-verification commands (run from the repo
root, `/Users/laith/Businesses/LandlordAI`):

| Claim | Re-verify with |
|---|---|
| Three pooler knobs still present in both places | `grep -n "statement_cache_size" apps/api/app/db/session.py apps/api/migrations/env.py` |
| RLS still ENABLE-not-FORCE with rationale | `grep -n "FORCE" apps/api/migrations/versions/0005_app_role_and_rls.py` |
| Admin-session allowlist test still exists | `grep -n "test_get_admin_session_referenced_only_by_allowlisted_files" apps/api/tests/test_migrations_0005.py` |
| Dedupe index migration intact | `grep -n "uq_notifications_message_dedupe" apps/api/migrations/versions/0006_notifications_message_dedupe_index.py` |
| Webhook still commit-first | `grep -n "commits in its own transaction" apps/api/app/routers/webhooks/twilio.py` |
| temperature still omitted + asserted | `grep -n "temperature" apps/api/app/integrations/anthropic.py apps/api/tests/test_integrations_anthropic.py` |
| Code-appends deferral architecture intact | `grep -n "_append_deferrals\|_strip_mandated_templates" apps/api/app/agent/nodes/draft_response.py` |
| A8–A13/A19 artifacts on main (merged via PR #177) | `git log --oneline -5 main \| grep 177` |
| JWKS consolidation intact | `grep -n "_JwksState\|reset_for_tests" apps/api/app/integrations/supabase_auth.py apps/api/tests/conftest.py` |
| Prefilter regression classes intact | `grep -c "class TestRegression" apps/api/tests/test_prefilter.py` |
| Open wounds still open | `for i in 174 176 170; do gh issue view $i --repo LaithAlz/stoop-backend --json number,state,title -q '"\(.number) [\(.state)] \(.title)"'; done` |
| Migration head (was 0008 as of 2026-07-05) | `ls apps/api/migrations/versions/` |
| Messages classification columns still deprecated / audit_log canonical | `grep -n "DEPRECATED v1.6" docs/03-engineering/schema-v1.md` |
| PR titles cited here | `gh pr view <N> --repo LaithAlz/stoop-backend --json title,state` |
