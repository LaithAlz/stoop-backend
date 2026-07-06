---
name: stoop-research-and-frontier
description: >
  Research discipline and the state-of-the-art frontier for Stoop (AI
  tenant-maintenance handling). Load this skill when: evaluating whether a
  proposed mechanism/root-cause explanation is actually proven; deciding if
  an idea deserves a paid eval gate; predicting eval results before a run or
  interpreting a gate's numbers; proposing/growing eval scenarios toward the
  50-scenario corpus; asked "can we claim X publicly", "zero missed
  emergencies", "safety record", or anything about beating the state of the
  art; prioritizing research bets (prompt caching, model routing,
  trust-ladder autonomy, corpus growth); retiring a failed idea; or asked
  "how do we know this is true" about any fix. Owns: the evidence bar, the
  hypothesis-predicts-numbers practice, the idea lifecycle
  (hunch → dry-run → paid gate → adopt-or-retire), and the
  zero-missed-emergency frontier with its falsifiable milestone.
---

# Stoop research discipline and the frontier

Stoop routes tenant SMS through a deterministic Tier-0 prefilter (pure
keyword/pattern functions, `apps/api/app/agent/prefilter.py`, no I/O, runs in
the Twilio webhook before any LLM) and an LLM pipeline governed by a frozen
severity rubric. This skill is the research doctrine: what counts as a proven
mechanism, how ideas earn adoption, and where this project can defensibly
beat the state of the art.

Jargon used throughout, defined once:

| Term | Meaning |
|---|---|
| **Tier-0 / prefilter** | Deterministic emergency keyword filter in the webhook handler; a "fire" means hard emergency triggers matched (`hard_hit=True`). |
| **The clamp** | Invariant in `apps/api/app/agent/nodes/classify_severity.py` (~line 307): the LLM may escalate past a Tier-0 miss but may NEVER de-escalate a Tier-0 fire; a clamp is recorded in `rules_fired` and logged as `classify_severity_tier0_clamp`. |
| **Rubric** | `docs/02-product/severity-rubric-v1.md`, frozen v1.0; `app/agent/rubric.py` is byte-identical, enforced by `tests/test_rubric.py::test_rubric_pinned_sha256`. |
| **Eval gate** | One numbered paid run of the full eval suite (`apps/api/evals/runner.py`): 3 classification samples per scenario at the eval's strict bar (flaky pass = fail), plus draft + LLM-judge calls. |
| **The corpus** | `apps/api/evals/scenarios/*.yaml` (11 LLM scenarios incl. `e4_prompt_injection_deescalate`) + `scenarios/negative_prefilter/n1–n9` (Tier-0 false-positive guards) = 20 scenarios (as of 2026-07-06, branch `test/eval-harness`). |
| **Judge** | LLM-as-judge draft grader, `apps/api/evals/judge.py` + `evals/scoring.py`. |

## When NOT to use this skill

- Shipping a change through gates/reviewers → **stoop-change-control**.
- Step-by-step prove-it recipes (bisection, live probes, judge cross-checks)
  → **stoop-proof-and-analysis-toolkit** (this skill states the bar; that one
  has the recipes with worked examples).
- What counts as test evidence, the golden test inventory →
  **stoop-validation-and-qa**.
- A specific past incident's full narrative → **stoop-failure-archaeology**.
- Debugging an active failure right now → **stoop-debugging-playbook**.
- Public copy / claims wording → **stoop-docs-and-writing** (its claims
  ledger governs anything said publicly, including the safety record).
- Executing the Train-1 core-loop plan → **stoop-core-loop-campaign**.

## The evidence bar for accepting a mechanism

A root-cause explanation is **accepted only when ONE mechanism explains ALL
observations — including the negatives (what did NOT fail) — and survives an
assigned adversarial refutation attempt** (a second agent/reviewer explicitly
tasked with breaking the explanation, not confirming it). "The fix made the
symptom go away" is not acceptance; it is a prediction that has not yet been
tested against the negatives.

Checklist before you write "root cause" anywhere:

1. State the mechanism in one sentence, at the level of code + state.
2. List every observation, including negatives. Check each against the
   mechanism. One unexplained observation = mechanism rejected or incomplete.
3. Assign an adversarial refutation: someone whose job is to reproduce the
   failure from the mechanism alone, or to produce a counterexample.
4. Only then: fix, and predict what the fix changes (next section).

Worked references (recipes live in **stoop-proof-and-analysis-toolkit**;
incident narratives in **stoop-failure-archaeology**):

- **Webhook silent message loss.** Twilio POST got a 200 but the message row
  vanished. Single mechanism: a `_safe_step` helper swallowed an exception
  inside the one shared transaction → the transaction aborted → the message
  INSERT rolled back while the handler still returned 200 (Twilio never
  retries a 200). It explained all observations: the 200 (handler survived),
  the missing row (rollback), no retry (200), and the negative — messages
  whose processing succeeded persisted fine. Both adversarial reviewers
  reproduced it independently pre-merge (session-verified 2026-07-05; no repo
  artifact of the failure — git history is forward-only). Surviving artifact:
  the commit-first contract in `apps/api/app/routers/webhooks/twilio.py`'s
  docstring (persist+commit the message before any processing; later failures
  return 5xx so Twilio's retry IS the recovery mechanism).
- **Judge verdict inversion.** The judge's prose reasoning said a draft
  PASSED while its boolean checklist said FAIL. Mechanism: free-form dict
  keys from the model mismatched the verbatim checklist keys → lookups
  defaulted to `False` — a silent inversion. It explained why only re-keyed
  items failed, why prose and booleans disagreed, and the negative: genuinely
  bad drafts still failed for stated reasons. Fix artifacts (branch
  `test/eval-harness`, commit e9ce472): `evals/scoring.py::
  _normalize_checklist_key` / `_lookup_checklist_item` and a DISTINCT
  `"NO MATCHING KEY"` failure that can never silently read as `False`.
  Standing triage rule: when the judge fails a draft, ALWAYS cross-check the
  `judge_reasoning` prose against the booleans in
  `apps/api/evals/results/last-run.json` — disagreement means eval-infra bug,
  not product bug.

## Predict the numbers before you run

Standing practice: **write the predicted numbers down before any run** —
eval gate, live probe, or load test. A hypothesis that does not commit to
numbers cannot lose, and a hypothesis that cannot lose is not evidence.

- **Gates are numbered runs and the results register.** Each paid run writes
  `apps/api/evals/results/last-run.json` unconditionally (even on crash
  paths) with `prompt_version`, `rubric_version`, per-scenario results, and a
  `summary` (`passed`/`failed`/`hard_failed_scenario_ids`/
  `errored_scenario_ids`/`release_blocked`). `--snapshot` also writes a named
  baseline — `evals/results/v1-baseline.json` is COMMITTED (commit `7fe8609`,
  root-`.gitignore` exception) as the gate-9 green baseline. The founding
  sessions numbered these runs gate 1…9; the arc ran 14/20 (gate 5) → 19/20
  → 18/20 → 18/20 → **20/20, `release_blocked=False` at gate 9 (2026-07-06,
  first fully green gate)**. Read `last-run.json`, never this paragraph, for
  the current verdict.
- **Each fix predicts which scenarios flip.** Example: commit 33441d2
  (refusal ack must not duplicate the appended hand-off) was shipped with the
  prediction "f1 flips to PASS, nothing else moves" — gate 7 FALSIFIED it
  (f1 failed again), which is the practice working: the different failure
  relocated the root cause to the v1 template copy itself → prompts v2
  (`11564c8`), then gate 8's residual "soon" + e4 variance → `31bd498`, then
  gate 9 verified the chain at 20/20 (full arc:
  `stoop-proof-and-analysis-toolkit` Recipe 8;
  incidents A13/A21/A22 in `stoop-failure-archaeology`).
- Same practice off the eval harness: the Supavisor prepared-statement fix
  (PR #165) was accepted on a live probe predicted to go from failures to
  0/100 after the third knob — observed 18/100 failures with two knobs,
  0/100 with three (session-verified 2026-07-05; surviving artifact:
  `apps/api/app/db/session.py::_ASYNCPG_POOLER_CONNECT_ARGS`).
- Infra failure is never a semantic result: `ScenarioInfraError` marks a
  scenario INCONCLUSIVE (re-run it), never a rubric miss — but it still sets
  `release_blocked` (`evals/runner.py`, `evals/scoring.py`).

## The idea lifecycle

Every idea takes this path. There is no route around change control.

1. **Hunch.** Write the mechanism and the predicted numbers first.
2. **Free harness test.** Costs nothing, needs no approval:
   ```bash
   cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner      # full harness, canned outputs, $0
   cd apps/api && uv run pytest -m "not eval" -q                    # unit/integration, never paid
   cd apps/api && uv run pytest -m eval --collect-only -q           # proves the paid gate collects, runs nothing
   ```
   `EVAL_DRY_RUN=1` is the one seam the design rests on
   (`apps/api/evals/types.py`); without it, `python -m evals.runner` and
   `pytest -m eval` hit the real Anthropic API and cost real money.
3. **Founder-gated paid gate.** Paid eval runs need the founder's go-ahead —
   a standing directive for a thread of work counts; agents never fire them
   autonomously. Paid — current cost/duration figures live in
   `stoop-change-control` rule 9 (token-budget pacing knob
   `EVAL_TOKEN_BUDGET_PER_MIN`, default 25000, in `evals/runner.py`).
4. **Threshold met → adopt THROUGH change control.** New prompt file
   `app/agent/prompts/v{n+1}.py` (existing versions are frozen — v1.py and
   v2.py exist), rubric changes mean a new rubric version + checksum update +
   full eval run, and everything ships via the /ship flow. Never around it —
   see **stoop-change-control**.
5. **Threshold missed → documented retirement.** A dead idea gets a
   **stoop-failure-archaeology** entry (what was tried, what number killed
   it) and an engineering-decisions entry (branch
   `docs/engineering-decisions` holds the decision record, unmerged as of
   2026-07-06). Undocumented retirement guarantees the idea is re-tried.

**Where good ideas historically came from** — aim new effort at these wells:

| Source | Pattern | Worked example (artifact) |
|---|---|---|
| Production/eval misses → new scenarios | Every miss becomes a scenario the same week (the corpus rule: `docs/04-roadmap/release-train.md` standing rules; `apps/api/CLAUDE.md` Testing) | e2 "smelled like gas" missed Tier-0 (pattern had "smell", not past tense) → verb-tense-completion sweep + regression test classes (commit f38d0f0; `tests/test_prefilter.py::TestRegressionBlocking*`) |
| Grader catches → hard guards | When the judge catches a life-safety hazard, promote it to a deterministic guard — never leave it to sampling | Judge caught a draft suggesting the oven for warmth (CO/fire hazard) → `_UNSAFE_HEAT_SOURCE_RE` reject-and-regenerate guard + `_UNSAFE_HEAT_SOURCE_GUIDANCE` (`app/agent/nodes/draft_response.py`, commit e9ce472) |
| Live probes → platform doctrine | Local Docker runs as superuser and is blind to privilege bugs; probe the real platform | Supavisor 3-knob asyncpg fix (`app/db/session.py`); live role-migration dry-run rule after migration 0004's `must be able to SET ROLE` failure (session-verified 2026-07-05; artifact: migration 0004's SECURITY DEFINER design) |
| Guard self-collisions → architecture | When safety machinery fights itself, separate generation from policy | Model paraphrasing refusal policy tripped its own compensation guard → code appends `REFUSAL_TEMPLATES` verbatim (`_append_deferrals`), model writes only a short ack (`app/agent/nodes/draft_response.py`) |

## THE FRONTIER: the zero-missed-emergency record

Founder-selected 2026-07-05 (session-verified; the supporting rules are in
repo): the thing Stoop can do that nobody else can defensibly claim is a
**public, auditable zero-missed-emergency record**. The Train-1 gate already
demands "zero missed emergencies" (`docs/04-roadmap/release-train.md`).

**Why current SOTA fails.** Generic LLM-triage products have:
1. **No deterministic floor** — one bad sample can miss a gas leak; sampling
   is the only line of defense.
2. **No never-de-escalate invariant** — nothing structurally prevents the
   model from talking itself out of an emergency.
3. **No auditable ledger** — decision logs are mutable app logs; a vendor
   claiming "we never missed one" cannot prove it.
4. **No production-fed regression corpus** — misses do not permanently
   raise the floor.

**Stoop's specific assets** (each verified in-repo):

| Asset | Artifact |
|---|---|
| Deterministic floor | `app/agent/prefilter.py` (`_HARD_TRIGGERS`, pure functions, runs in the webhook before any LLM); change discipline is monotonic-additive with a regression test class per change (`tests/test_prefilter.py`) |
| The clamp | `app/agent/nodes/classify_severity.py` ~line 307 — LLM may only escalate past Tier-0, never below it |
| Tamper-evident ledger | `messages` + `audit_log` are append-only: `REVOKE UPDATE, DELETE` in migrations 0002 and 0005 (`migrations/versions/0005_app_role_and_rls.py` line 295); Tier-0 fires write `audit_log` rows `actor='prefilter', action='emergency_triggered'` and the prefilter snapshot is persisted on the `messages` row (`app/routers/webhooks/twilio.py`); classifications write `actor='agent', action='classified'`; `emergency_call`/`needs_eyes` notifications are deduped by `uq_notifications_message_dedupe` (migration 0006) and never deleted |
| Frozen-rubric + versioned-prompt + eval-gate discipline | `tests/test_rubric.py::test_rubric_pinned_sha256`; frozen `app/agent/prompts/v*.py`; release-train standing rule "Prompt/rubric changes = new version + full eval run (CI-enforced)" |
| The corpus is the moat | Growth rule in `docs/02-product/eval-scenarios-v1.md` ("every production misclassification becomes scenario #11, #12, … The corpus is the moat") and `docs/03-engineering/architecture.md` §9 ("Observability, evals, and the data moat") — a competitor can copy prompts; they cannot copy a year of real classified misses |

Honest current state (as of 2026-07-06): the prefilter and clamp are built;
the LangGraph pipeline is not yet wired into the production webhook (#34
open), the eval harness merged in PR #177 (gate 9 green at 20/20, but
unmerged), and there is no production traffic yet. The record is a frontier,
not a fact.

### First three concrete steps, in this repo

1. **Merge the eval harness and wire it into CI.** Branch
   `test/eval-harness` (= main + 7 commits: f38d0f0, 1d2c793, e9ce472,
   33441d2, 11564c8, 31bd498, 7fe8609 — as of 2026-07-06; clean tree; gate 9
   GREEN at 20/20; merged as PR #177, 2026-07-06) carries the 20-scenario
   harness and gate discipline. Then
   #73 (open): add the full eval run to GitHub Actions for
   prompt/rubric/prefilter-touching PRs — today `.github/workflows/ci.yml`
   runs only `uv run pytest -m "not eval"`, so nothing enforces the gate
   automatically.
2. **Grow the corpus toward the 50-scenario gate** (#68 expand, #69 pass at
   thresholds — both open). Prioritize emergency paraphrase/tense/typo
   families (the e2 lesson: "smelled" vs "smell") and negative guards in the
   `negative_prefilter/` pattern (n1–n9 are all fire/alarm-word
   false-positive guards: smoke-detector battery, CO-alarm battery, fire
   drill, fire escape…). The #176
   precision pass (open: water active-flow + gas-leak false-positive surface)
   is part of the same claim — a cry-wolf emergency line trains landlords to
   ignore the ring, which is how a real one gets missed. Precision is not a
   nice-to-have; it is load-bearing for the record.
3. **Build the missed-emergency ledger once production traffic exists**
   (unbuilt; design sketch). A standing query over the append-only tables,
   reviewed weekly: join `audit_log` Tier-0 fires
   (`actor='prefilter' AND action='emergency_triggered'`), `audit_log`
   classifications (`actor='agent' AND action='classified'`, with clamp
   evidence in the payload's `rules_fired`), `notifications`
   (`type='emergency_call'`), and the `messages.prefilter` snapshots. The
   third input — landlord-marked severity corrections — has NO schema today;
   per project rule 6, that means amending
   `docs/03-engineering/schema-v1.md` first, then a migration. Every miss
   found becomes an eval scenario the same week, per the existing corpus
   rule.

### You have a result when (falsifiable milestone)

All of the following, and NOT before:

- **≥ N production messages processed, N ≥ 10,000** (pick and pre-register N
  before the counting window starts);
- **ZERO rubric-EMERGENCY messages reached a landlord without the emergency
  path firing** (Tier-0 fire OR clamp), where "zero" is verified from the
  append-only ledger **by an adversarial audit** — someone assigned to find
  a miss, not to confirm the absence of one;
- the then-current corpus (**≥ 50 scenarios**) is green in CI.

Any public claim earlier than this violates the claims-ledger discipline in
**stoop-docs-and-writing** (which also owns the wording rules: plain English,
never "triage", no invented counts). Until the milestone, the only honest
sentence is "designed so an emergency can't be silently downgraded" — a
design claim, not a record claim.

## Candidate frontiers (candidate — not committed)

| Candidate | Idea | Blocking evidence gap |
|---|---|---|
| Trust-ladder autonomy as a general pattern | #60 (open: trust metrics + graduation): autonomy earned per `(property, severity)` from real approval history — potentially a generalizable "earn autonomy from approval history" pattern beyond Stoop | Needs months of real landlord-approval data; the roadmap explicitly classes this as "physics, not priorities" (`docs/04-roadmap/release-train.md`) — no data exists until the core loop is live |
| Cost/latency frontier | #70 (open: prompt caching for the system prompt block); model routing (cheap model for obvious routine, strong model for ambiguous) | No production traffic → no real cost/latency baseline; the current cost table (`_INPUT_PRICE_PER_MTOK_USD` $3.00 / `_OUTPUT_PRICE_PER_MTOK_USD` $15.00 in `app/integrations/anthropic.py`) is a conservative placeholder, so any savings number computed today is fiction. Routing additionally must prove it never touches the emergency path (rule 1 / flags-never-gate-safety) |

Treat anything in this table as a hunch at step 1 of the lifecycle: it earns
a dry run before it earns a gate, and a gate before it earns adoption.

## Provenance and maintenance

Volatile claims and one-line re-verification commands (run from the repo
root unless noted). Date-stamped facts above are as of 2026-07-06.

| Claim | Re-verify with |
|---|---|
| Eval harness merged (PR #177, 2026-07-06) | `gh pr view 177 --repo LaithAlz/stoop-backend --json state,mergedAt` |
| Corpus = 11 LLM + 9 negative scenarios | `ls apps/api/evals/scenarios/*.yaml \| wc -l; ls apps/api/evals/scenarios/negative_prefilter/ \| wc -l` |
| Clamp exists and never de-escalates | `grep -n "Tier-0 clamp" apps/api/app/agent/nodes/classify_severity.py` |
| Append-only REVOKEs in migrations | `grep -rn "REVOKE UPDATE, DELETE" apps/api/migrations/versions/` |
| Rubric checksum enforcement | `grep -n "test_rubric_pinned_sha256" apps/api/tests/test_rubric.py` |
| CI still excludes paid evals (#73 open) | `grep -n "not eval" .github/workflows/ci.yml` |
| Issues #73/#68/#69/#60/#70/#176/#34 still open | `gh issue list --repo LaithAlz/stoop-backend --state open \| grep -E "^(73\|68\|69\|60\|70\|176\|34)\b"` |
| EVAL_DRY_RUN seam + always-written report | `grep -n "EVAL_DRY_RUN\|LAST_RUN_PATH" apps/api/evals/runner.py` |
| Tier-0 fires land in audit_log | `grep -n "emergency_triggered" apps/api/app/routers/webhooks/twilio.py` |
| Prompt versions frozen (v1, v2, …) | `ls apps/api/app/agent/prompts/` |
| Judge NO-MATCHING-KEY failure distinct | `grep -n "NO MATCHING KEY" apps/api/evals/scoring.py` |
| Notification dedupe index | `grep -rn "uq_notifications_message_dedupe" apps/api/migrations/versions/` |
