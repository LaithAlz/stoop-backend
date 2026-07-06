---
name: stoop-proof-and-analysis-toolkit
description: >
  First-principles "prove it, don't just install it" recipes for the Stoop repo —
  load this skill whenever you are about to trust a claim without a reproduction:
  applying a documented fix or recommended config for an intermittent error; trusting
  an idempotency / dedupe / exactly-once claim (webhooks, notifications, retries);
  reviewing any sweeper or cron that SELECTs then UPDATEs (TOCTOU risk); writing or
  reviewing a migration that touches roles/grants/RLS on Supabase; merging a
  high-stakes safety claim ("never lose a message") on the author's word; reacting to
  an automated grader or LLM-judge failure; encoding an "every X has Y" invariant;
  or planning any measurement run (eval gate, probe loop, race test). Each recipe is
  WHEN → numbered RECIPE → worked example from this repo's real history (with the real
  numbers: 18/100→0/100 pooler probe, 3/3 duplicate escalations, the 14/20→20/20 eval
  gate arc) → what would have happened without it. Also owns the "assign a prober,
  don't argue from docs" doctrine.
---

# Stoop proof and analysis toolkit

Doctrine: **a claim about system behavior is worth exactly as much as its
reproduction.** Documentation, code review, and local Docker all lied to this project
at least once in its first month. Every recipe below converted an argument into a
count, and every count either confirmed the fix or falsified a "known-good" recipe.
Git history here is clean and forward-only — the failures were caught before merge,
so the repo records the fixes but not the near-misses. The worked examples are those
near-misses.

Repo root: the monorepo at `/Users/laith/Businesses/LandlordAI`; the backend is
`apps/api` (Python 3.12 / FastAPI / LangGraph, uv-managed). All commands below are
repo-relative.

## When NOT to use this skill

| You need | Go to |
|---|---|
| The full incident record (symptom → root cause → status) | `stoop-failure-archaeology` |
| Live triage of a symptom you're seeing right now | `stoop-debugging-playbook` |
| What counts as merge evidence, test markers, eval doctrine | `stoop-validation-and-qa` |
| How to run/migrate/operate, live-DB discipline | `stoop-run-and-operate` |
| Gates, reviewer matrix, how changes ship | `stoop-change-control` |
| Shipped measurement scripts and how to read their output | `stoop-diagnostics-and-tooling` |
| Supabase platform facts as a reference table | `stoop-domain-reference` |
| The invariants themselves (what must stay true) | `stoop-architecture-contract` |

This skill owns the reusable proof **methods** — how to turn "I believe" into a number.

## Ground rules for every probe (non-negotiable)

1. Paid eval runs (`pytest -m eval`, `python -m evals.runner` without `EVAL_DRY_RUN=1`)
   need the founder's go-ahead — never fire them autonomously. Paid, founder-gated —
   current cost/duration figures live in `stoop-change-control` rule 9.
2. Live-database probes are operator-gated actions — see `stoop-run-and-operate`.
   Never `source .env`, never print secret values.
3. Safe local pytest default: `uv run pytest -m "not eval"` — `pyproject.toml` has no
   `addopts` guard, so bare `pytest` would collect the paid eval tests.
4. Export `DATABASE_URL` in the SAME shell invocation as the test command
   (env does not survive across tool calls). Local DB from repo-root
   `docker compose up -d` is `postgresql+asyncpg://stoop:stoop@localhost:5432/stoop`.

---

## Recipe 1 — the live-probe count

**WHEN:** an intermittent error, and you are about to apply a fix taken from
documentation, a blog, or another engineer's memory. Any bug whose natural
description is a frequency ("sometimes fails").

**RECIPE:**
1. State the hypothesis as a count before touching anything: "with config X, roughly
   N out of 100 identical requests fail; with fix Y, 0 out of 100."
2. Build a minimal reproduction loop: the same request, 100 iterations, against the
   real system that exhibits the bug — not a simulation, not local Docker if the bug
   is platform-specific.
3. Run the baseline. Record the failure count.
4. Apply ONE candidate fix. Re-run the identical loop. Record again.
5. Any non-zero count after the fix means the fix is insufficient — find the missing
   mechanism; do not stack a second knob without re-counting each layer.
6. Preserve the mechanism (not the numbers) in a comment block at the fix site.

**WORKED EXAMPLE — Supavisor prepared statements (PR #165).** Intermittent
`DuplicatePreparedStatementError` against Supabase's transaction pooler (Supavisor,
port 6543, which multiplexes many clients onto shared Postgres backends). SQLAlchemy's
*documented* 2-knob recipe (`prepared_statement_cache_size=0` + a UUID name func) was
applied — and the probe loop still failed **18/100**. That count falsified the
documented recipe and forced a real root-cause: `pool_pre_ping`'s ping bypasses the
SQLAlchemy dialect layer and uses asyncpg's own statement cache, so a third,
asyncpg-level knob `statement_cache_size=0` is required. With all three knobs:
**0/100** (probe numbers session-verified 2026-07-05; no repo artifact). The three
knobs and the full mechanism live in
`apps/api/app/db/session.py::_ASYNCPG_POOLER_CONNECT_ARGS` (comment block above it)
and are duplicated in `apps/api/migrations/env.py`. Never remove any of the three.

**WITHOUT IT:** the 2-knob config ships "per documentation", roughly 1 in 5 production
requests fails intermittently, and the failures get blamed on network flakiness for
weeks — because the config visibly matches the official docs.

---

## Recipe 2 — concurrency race reproduction

**WHEN:** anyone claims idempotency, dedupe, or exactly-once behavior — webhook
handlers (Twilio retries!), notification fan-out, "the code checks for an existing
row first". Trust NO idempotency claim that has not survived a same-payload race.

**RECIPE:**
1. Take the EXACT same payload (same `twilio_sid`, same message id).
2. Fire it N ways concurrently: `asyncio.gather` of N coroutines in a test, or N
   parallel HTTP requests. Sequential replay is NOT a race test.
3. Count the resulting side effects (rows, notifications, outbound sends). Anything
   greater than 1 is a failure, even at N=2.
4. Fix at the DATABASE layer, not the code layer: a (partial) unique index +
   `INSERT ... ON CONFLICT DO NOTHING RETURNING id`, with every side effect gated on
   whether the RETURNING row came back. Application-level select-then-insert checks
   cannot win a race across processes.
5. Re-run the identical N-way race; assert exactly one effect. Keep that race as a
   permanent integration test.

**WORKED EXAMPLE — duplicate emergency escalations (#40).** Concurrent replay of the
same Twilio webhook produced duplicate `emergency_call` notifications: **3/3
duplicates in a 3-way race, 28/30 across a burst** (session-verified 2026-07-05; no
repo artifact). Fix: migration
`apps/api/migrations/versions/0006_notifications_message_dedupe_index.py` creates the
partial unique index `uq_notifications_message_dedupe` on
`((payload ->> 'message_id'), type) WHERE type IN ('emergency_call', 'needs_eyes')`;
the webhook (`apps/api/app/routers/webhooks/twilio.py`) inserts with
`ON CONFLICT ((payload ->> 'message_id'), type) WHERE ... DO NOTHING RETURNING id`
and fires the alert side effect only when a row is returned. The surviving race test
is `tests/test_migrations_0006.py::test_concurrent_overlapping_inserts_exactly_one_wins`
(two `asyncio.gather`ed attempts on separate connections; asserts exactly one row).
`emergency_call`/`needs_eyes` rows are never deleted — the index is the permanent
idempotency anchor.

**WITHOUT IT:** the landlord's phone rings three times at 3 a.m. for one gas leak.
Select-then-insert "duplicate checks" pass code review every time and lose the race
in production, where Twilio's retries genuinely overlap.

---

## Recipe 3 — TOCTOU interleaving proof

**WHEN:** any sweeper, cron, or batch job that reads rows, decides, then writes
(TOCTOU = time-of-check-to-time-of-use: the state can change between the check and
the use). Also any "load object, mutate, save" over rows other actors touch.

**RECIPE:**
1. Structure (or exploit the existing structure of) the job as two separately
   callable phases: a pure DECISION phase and a WRITE phase. That split is the test
   seam — you can inject a contradiction between them without sleeps or threads.
2. In an integration test: run the decision phase to get a would-be action; then
   apply the feared concurrent write directly to the DB (the "contradiction"); then
   run the write phase with the now-stale action.
3. Assert the stale write no-ops completely: `rowcount == 0`, state exactly as the
   contradiction left it, and NO downstream side effects (no audit row for a
   resolution that never happened).
4. Fix = self-guarding UPDATE: the WHERE clause re-checks every precondition at
   write time, and all side effects are gated on `rowcount == 1`. A lost race is a
   deliberate, silent no-op — the other writer was more current.
5. Re-run the interleaving test against the guarded write; assert the no-op.

**WORKED EXAMPLE — case-sweep TOCTOU (#110, PR #173).** A tenant contradiction
("actually it's still broken") arriving between the sweeper's SELECT and UPDATE was
overwritten — the case auto-resolved anyway (session reproduction; the code comment
in `apps/api/app/agent/case_lifecycle.py` records it as "proven live"). Fix in the
same file: the resolve UPDATE carries
`WHERE id = :case_id AND pending_resolved_at IS NOT NULL AND pending_resolved_at <= :now`
(a concurrent contradiction sets `pending_resolved_at` back to NULL, so the stale
write matches zero rows); audit and `needs_eyes` writes are gated on `rowcount == 1`;
`awaiting_approval` cases are excluded from auto-stale entirely. The surviving
interleaving proofs:
`tests/test_agent_nodes.py::test_sweep_toctou_contradiction_between_select_and_update_is_not_overwritten`
and `...::test_sweep_toctou_new_message_between_select_and_update_prevents_auto_stale`
— both use the decision/write split (`apply_time_transitions` then
`_apply_sweep_action`) to plant the contradiction between the phases.

**WITHOUT IT:** the tenant says the leak is back, the case resolves anyway, and the
append-only `audit_log` permanently records a resolution that never happened —
silent corruption in the one table that exists to be trusted.

---

## Recipe 4 — privilege probing on a managed platform

**WHEN:** any migration touching roles, grants, RLS (row-level security), or
triggers on Supabase — or any claim of the form "the postgres role can do that".

**RECIPE:**
1. Never trust local Docker for privilege claims: it runs migrations as a bootstrap
   superuser and is structurally blind to privilege bugs. (Standing, founder-elevated
   rule: any role/grant/RLS migration gets a live Supabase dry-run BEFORE merge.)
2. Read-only catalog probes first — zero risk:
   `SELECT rolname, rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname IN ('postgres','service_role');`
3. Only then, throwaway-role mutation probes: `CREATE ROLE probe_x NOLOGIN;` → try the
   exact operation the migration needs → `DROP ROLE probe_x;`. Never mutate real roles
   or data. (Live connections themselves are operator-gated — `stoop-run-and-operate`.)
4. Document EVERY surprising result at the decision site — the migration's docstring,
   not a scratch note. The docstring of migration 0004 is the model.
5. Treat a guard that cannot fail as falsified: probe the guard's negative case too.

**WORKED EXAMPLE — the three Supabase discoveries (live-probed 2026-07-04 on the real
project).** Migration 0004 (auth.users trigger) was green on local Docker and failed
on live Supabase with `must be able to SET ROLE`. Probing found:
1. `postgres` on Supabase is NOT superuser, but has `rolbypassrls = TRUE`
   (`service_role` likewise) (session-verified 2026-07-04; probe transcript has no
   repo artifact — the conclusions live in migration docstrings).
2. `pg_has_role(current_user, r, 'MEMBER')` returns TRUE immediately after
   `CREATE ROLE r` on PG16+ (implicit ADMIN OPTION) — falsified as a
   membership/idempotency guard: it would have "passed" while guarding nothing.
   Recorded in `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py`'s
   docstring.
3. `GRANT <role> TO CURRENT_USER` executed as postgres **terminates the connection
   mid-operation** (reproduced on both pooler ports). Also recorded in the 0004
   docstring.
Result: the design was rebuilt around a postgres-owned `SECURITY DEFINER` function
set with `SET search_path = public, pg_temp` pinned (the classic SECURITY DEFINER
injection defense), and zero custom-role membership grants involving postgres.

**WITHOUT IT:** migration 0004 merges green and detonates on live — first-login
provisioning down — while its pg_has_role "guard" reports everything is fine.

---

## Recipe 5 — adversarial refutation

**WHEN:** a high-stakes claim is about to be merged on the author's word: "never
lose a message", "safe under retry", "idempotent", "cannot leak cross-tenant".

**RECIPE:**
1. Assign the reviewer a REFUTATION TARGET, not a review: "REFUTE THIS: prove a
   message can be lost while Twilio sees a 200." A reviewer told to "review" skims;
   a reviewer told to break one specific claim builds an attack.
2. The deliverable is a REPRODUCTION (failing script or test), not an opinion.
   "Looks right to me" is a null result — it means the attack failed or wasn't tried,
   and the assignment should say which.
3. Two independent refuters beat one; two independent reproductions of the same
   failure are as close to proof as pre-production gets.
4. The fix must convert the reproduction into a permanent regression test or an
   enforced, documented contract at the code site.

**WORKED EXAMPLE — silent message loss (#40, PR #171).** Claim under review: the
Twilio webhook never loses a message. Both adversarial reviewers, independently,
reproduced the same loss: a `_safe_step` helper swallowed an exception inside the ONE
shared transaction → the transaction aborted → the message INSERT rolled back while
the handler still returned 200 — and Twilio never retries a 200 (session-verified
reproduction 2026-07-05; no repo artifact of the failing scripts). Fix: COMMIT-FIRST
persistence — `INSERT ... ON CONFLICT (twilio_sid) DO NOTHING RETURNING id` and
commit immediately, BEFORE any processing; failures after the commit return 5xx so
that Twilio's retry becomes the recovery mechanism. The contract is spelled out in
the module docstring of `apps/api/app/routers/webhooks/twilio.py` ("COMMIT
IMMEDIATELY"). Never wrap webhook persistence and processing in one transaction.

**WITHOUT IT:** tenant messages vanish in production with a 200 in Twilio's logs —
unretried, undetectable, and a direct violation of the never-break rule "Never lose
a message".

---

## Recipe 6 — judge/grader cross-examination

**WHEN:** an automated grader — the eval harness's LLM judge (an LLM that grades
drafted replies against a checklist), a CI gate, any scorer — fails something and
your next move is "fix the product".

**RECIPE:**
1. Pull the grader's REASONING and its VERDICT separately. For Stoop evals: the
   `judge_reasoning` prose vs the boolean checklist dicts, both recorded per scenario
   in `apps/api/evals/results/last-run.json`.
2. If prose and booleans disagree, it is an eval-infra bug, not a product bug. Stop.
   Fix the harness. Zero product changes until the grader is self-consistent.
3. Root-cause the disagreement mechanically — the usual suspects: key mismatch
   between prompt checklist and returned dict, silent defaults on missed lookups,
   single-key wrapper nesting around the tool payload.
4. Make "no answer" distinct from "false": a missed checklist lookup must be its own
   loud failure, never a default False.

**WORKED EXAMPLE — the gate-5 verdict inversion.** The judge's prose said a draft
PASSED; its boolean checklist said FAIL, hard-failing scenarios the judge itself
called good. Two mechanical causes: (1) checklist items were quoted as bullet strings
in the judge prompt, so the model re-keyed them loosely; (2) the scorer looked up
verbatim keys in a free-form dict and defaulted misses to False — a silent inversion.
Three-layer fix, all repo-verifiable: tolerant matching via
`_normalize_checklist_key` / `_lookup_checklist_item` in `apps/api/evals/scoring.py`;
a DISTINCT "NO MATCHING KEY" failure (grep it in `scoring.py` — never silently
False); an unquoted, numbered checklist in the judge prompt with keys copied
character-for-character (`apps/api/evals/judge.py`), plus the same single-key
wrapper-unwrap the product schemas use (`_unwrap_single_key_wrapper`,
`apps/api/app/agent/schemas.py`, reused by `JudgeVerdict`). Standing triage rule:
when the judge fails a draft, cross-check `judge_reasoning` against the booleans in
`last-run.json` FIRST — disagreement means harness bug.

**WITHOUT IT:** good drafts get "fixed" to satisfy a broken grader — real product
quality degrades to chase phantom failures, at a real paid-gate cost per lap
(figures: `stoop-change-control` rule 9).

---

## Recipe 7 — proof by construction (inventory tests)

**WHEN:** an invariant is phrased as "every X has Y", "only Z may do W", or "A is
byte-identical to B". Anything that drifts silently, one innocent PR at a time.

**RECIPE:**
1. Restate the invariant as an enumeration over REALITY: a catalog query, a
   filesystem grep, a checksum — not over a list someone promises to maintain.
2. Pair every hardcoded pin with a completeness check against the live population,
   so additions cannot slip past the pin (a 14th table must trip the "exactly 13"
   test AND the catalog diff).
3. Write the failure message as instructions: which doc to amend first, which
   allowlist to extend knowingly, which version file to create.

**WORKED EXAMPLES (all repo-verifiable, all in `apps/api`):**
- **Rubric checksum** — `tests/test_rubric.py`: `RUBRIC_V1` must be byte-identical to
  the verbatim block in `docs/02-product/severity-rubric-v1.md`
  (`test_rubric_matches_doc_verbatim`) AND hash to a pinned sha256
  (`test_rubric_pinned_sha256`). Drift in either the doc or the code fails loudly;
  changing behavior requires a new rubric version + full eval run (never-break rule 4).
- **Admin-session allowlist** —
  `tests/test_migrations_0005.py::test_get_admin_session_referenced_only_by_allowlisted_files`:
  enumerates every reference to `get_admin_session` (the RLS-bypassing engine) in the
  codebase; a new caller must be added to the allowlist deliberately, in review.
- **RLS matrix** — `tests/test_rls_isolation_matrix.py`: pins exactly 13 tables
  (`test_table_descriptors_cover_exactly_thirteen_tables`), proves the descriptor set
  equals the actual `public`-schema catalog
  (`test_descriptor_table_set_matches_public_schema_catalog`,
  `test_no_tables_outside_descriptor_set_exist_in_public_schema`), then runs
  SELECT/UPDATE/DELETE/INSERT cross-tenant matrices over every table. A new table
  without RLS cannot land silently.

**WITHOUT IT:** invariants decay invisibly — a 14th table ships without RLS, a
convenience call bypasses tenant isolation, the rubric drifts from the doc it's sworn
to match — each undetected until it is an incident.

---

## Recipe 8 — hypothesis-predicts-numbers

**WHEN:** before ANY measurement run: an eval gate (the paid 20-scenario run — 11
LLM scenarios in `apps/api/evals/scenarios/*.yaml` + 9 Tier-0-only negatives in
`evals/scenarios/negative_prefilter/`), a probe loop, a race test, a perf check.
(Tier-0 = the deterministic regex emergency prefilter in `app/agent/prefilter.py`,
run before any LLM.)

**RECIPE:**
1. BEFORE running, write the predicted observation down: expected pass/fail count,
   WHICH scenarios flip and why, expected failure mode of any that stay red.
2. Run once.
3. Three outcomes:
   - matches prediction → hypothesis survives;
   - BETTER than predicted → investigate anyway (you don't understand why it passed);
   - worse, or a DIFFERENT failure than predicted → investigate that specific new
     failure. A new failure after a fix is signal: the fix changed behavior somewhere
     you didn't model.
4. Never re-run hoping for different numbers without a changed hypothesis — the eval
   doctrine is flaky = fail (see `stoop-validation-and-qa`). Infra errors are the one
   exception: a `ScenarioInfraError` (`evals/runner.py`) marks the scenario
   INCONCLUSIVE — re-run it; it is never scored as a rubric miss, but it still blocks
   release until resolved (`evals/scoring.py`).

**WORKED EXAMPLE — eval gates 5 → 6 → 7 → 8 → 9 (the full arc: 14/20 → 19/20 →
18/20 → 18/20 → 20/20 GREEN, closed 2026-07-06).** Gate 5 scored **14/20**. Instead
of prompt roulette, every failure was root-caused and a flip-list predicted:
wrapper-key nesting (fixed by `_unwrap_single_key_wrapper`), the judge inversion
(Recipe 6), an oven-for-warmth hazard in a no-heat draft (now the
`_UNSAFE_HEAT_SOURCE_RE` hard guard in `app/agent/nodes/draft_response.py`), and
guard/deferral self-collision (now the code-appends architecture: model writes a
short ack, code appends `REFUSAL_TEMPLATES` verbatim via `_append_deferrals`).
Gate 6 scored **19/20** — the predicted flips landed AND one NEW specific failure
appeared: f1-rent-ltb, where the refusal ack duplicated the appended hand-off
because the instruction ITSELF mandated the duplication. That earned its own
targeted fix (commit 33441d2), shipped with the prediction "f1 flips, nothing else
moves". **Gate 7 (18/20) falsified that prediction** — f1 hard-failed again, and
per step 3 the DIFFERENT failure was the signal: the root cause moved to the v1
REFUSAL_TEMPLATES' own legalistic copy, producing the founder-approved templates-only
prompts v2 (commit 11564c8). **Gate 8 (18/20)** narrowed it again — f1's only
remaining finding was the single relative-time word "soon" in the v2 template, and
e4 went INFRA on a third output-shape variance (a per-flag boolean dict + an
invented `vulnerable_occupant_modifier_applied` bool). Commit 31bd498 fixed both;
notably, the two schema coercions had to be composed into ONE `mode="before"`
validator with explicit unwrap-then-absorb sequencing, because Pydantic runs
multiple before-validators in REVERSE definition order — a composition test
(`tests/test_agent_schemas.py::test_severity_result_wrapper_plus_gate8_variances_compose`)
caught the two-validator version silently breaking when the variances stacked.
**Gate 9: 20/20, `release_blocked=False`** (`last-run.json` generated
2026-07-06T06:43Z; baseline committed as `evals/results/v1-baseline.json`, commit
7fe8609). Four iterations, each predicting its numbers, each investigation moving
one mechanism closer. The verification run, not the prediction, is the arbiter;
`last-run.json` is overwritten every run, so date-stamp anything you quote from it.

**WITHOUT IT:** "14/20 — the model needs work" is a plausible story that would have
triggered scattershot prompt-tweaking; the judge inversion would never have been
found, because a broken grader and a weak model produce identical-looking scores.

---

## Assigning proofs — spawn a prober, don't argue from docs

Proofs are cheap on Sonnet-class agents; wrong beliefs are expensive in incidents.
When two agents (or an agent and a document) disagree about behavior, neither wins by
citation — assign one to build the reproduction.

1. **Fit the model split** (founder-elevated rule): the frontier model plans,
   orchestrates, and safety-reviews; Sonnet-class agents implement. "Write this probe
   loop, run it, report the counts" is a perfectly-shaped Sonnet assignment.
2. **Assignment template** — give the prober all five:
   - the hypothesis, phrased as a falsifiable count;
   - the exact probe procedure (Recipes 1–4 above);
   - the predicted numbers (Recipe 8);
   - the artifact to leave behind (a regression test or a docstring at the decision
     site — never a scratch note);
   - what NOT to touch (real roles, real data, secrets, paid endpoints).
3. **Prober constraints** (all standing rules): one active agent per working tree —
   parallel work uses `git worktree add`; paid eval runs need the founder's
   go-ahead; live-DB probes are operator-gated; never `source .env`, never print
   secrets; probes that mutate use throwaway objects only.
4. **A null result is a result.** "I could not reproduce it in 100 tries under these
   exact conditions" is publishable; "it looked fine" is not.

---

## Provenance and maintenance

Date-stamped facts above are as of 2026-07-05/06. Re-verify drift-prone claims
before relying on them (run from the repo root unless noted):

| Claim | One-line re-verification |
|---|---|
| Three asyncpg knobs present in both engines | `grep -n "statement_cache_size" apps/api/app/db/session.py apps/api/migrations/env.py` |
| 0006 partial unique index name + predicate | `grep -n "uq_notifications_message_dedupe" apps/api/migrations/versions/0006_notifications_message_dedupe_index.py` |
| Webhook side effects gated on ON CONFLICT ... RETURNING | `grep -n "RETURNING id" apps/api/app/routers/webhooks/twilio.py` |
| Race test still exists | `grep -n "test_concurrent_overlapping_inserts_exactly_one_wins" apps/api/tests/test_migrations_0006.py` |
| Self-guarding sweep UPDATE | `grep -n "pending_resolved_at IS NOT NULL AND pending_resolved_at <=" apps/api/app/agent/case_lifecycle.py` |
| TOCTOU interleaving tests | `grep -n "toctou" apps/api/tests/test_agent_nodes.py` |
| 0004 records pg_has_role trap + connection termination | `grep -n "pg_has_role\|terminates the connection" apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py` |
| Judge "NO MATCHING KEY" distinct failure | `grep -n "NO MATCHING KEY" apps/api/evals/scoring.py` |
| Wrapper-unwrap shared by product + judge | `grep -rn "_unwrap_single_key_wrapper" apps/api/app/agent/schemas.py apps/api/evals/judge.py` |
| Gate-8 variance coercions compose in ONE before-validator (Pydantic reverse-order hazard) | `grep -n "REVERSE definition order" apps/api/app/agent/schemas.py; grep -n "wrapper_plus_gate8_variances_compose" apps/api/tests/test_agent_schemas.py` |
| Rubric checksum tests | `cd apps/api && uv run pytest tests/test_rubric.py --collect-only -q` |
| Admin-session allowlist test | `grep -n "test_get_admin_session_referenced_only_by_allowlisted_files" apps/api/tests/test_migrations_0005.py` |
| RLS matrix pins 13 tables + catalog completeness | `grep -n "thirteen_tables\|matches_public_schema_catalog" apps/api/tests/test_rls_isolation_matrix.py` |
| Scenario census (11 LLM + 9 negatives) | `ls apps/api/evals/scenarios/*.yaml \| wc -l && ls apps/api/evals/scenarios/negative_prefilter \| wc -l` |
| Latest gate result (volatile — overwritten every run) | `python3 -c "import json;d=json.load(open('apps/api/evals/results/last-run.json'));print(d['generated_at'],d['summary'])"` |
| Free dry-run of the eval harness (no API calls) | `cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner` |
| Unsafe-heat-source hard guard + code-appended deferrals | `grep -n "_UNSAFE_HEAT_SOURCE_RE\|_append_deferrals" apps/api/app/agent/nodes/draft_response.py` |
| Tense-sweep regression class | `grep -n "TestTenseCompletenessSweepGasSmelled" apps/api/tests/test_prefilter.py` |
| Product model id | `grep -n "^MODEL" apps/api/app/integrations/anthropic.py` |
