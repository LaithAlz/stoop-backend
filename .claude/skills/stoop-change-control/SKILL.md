---
name: stoop-change-control
description: How a change becomes code in the Stoop monorepo (LandlordAI). Load this before creating a branch or PR, running the /ship flow, deciding whether a change may push straight to main (docs) or must go branch→PR→CI→review→squash-merge (app code), choosing which reviewers are mandatory (spec-guardian, safety-reviewer, copy-guardian), touching anything frozen (rubric.py, severity-rubric-v1.md, prompts/v1.py, prompts/v2.py, eval scenarios, prefilter patterns), merging a PR (CI green on the tip commit, headSha check, squash-merge), or answering questions like "can I just push this", "do I need a safety review", "how do I change the rubric or prompt", "who approves this", "can I run the paid evals", "what commit message style", "which branch prefix". Also covers the 8 project never-break rules and the 4 founder-elevated hard rules with the incidents behind them.
---

# Stoop change control — how a change becomes code

Repo: `/Users/laith/Businesses/LandlordAI` (monorepo: `apps/api` FastAPI backend, `apps/web` TanStack Start frontend, `docs/` source of truth). GitHub remote: `LaithAlz/stoop-backend`. This skill is the single home for shipping process, review gates, never-break rules, and frozen artifacts. Facts marked "(session-verified 2026-07-05; no repo artifact)" come from the founding engineering sessions and exist nowhere else — do not delete them when editing this file.

## When NOT to use this skill

| You actually need | Go to |
|---|---|
| Full incident write-ups (root cause, evidence, reproduction) | `stoop-failure-archaeology` |
| Test/eval evidence discipline, what counts as proof, golden inventory | `stoop-validation-and-qa` |
| Running migrations, live-DB discipline, operator flips | `stoop-run-and-operate` |
| Recreating the dev environment, shell/env traps | `stoop-build-and-env` |
| Docs-of-record amendment rules, copy/public-claims detail | `stoop-docs-and-writing` |
| Symptom-driven triage of a failure you're seeing now | `stoop-debugging-playbook` |
| Rubric doctrine, Supabase platform facts, SMS/LLM-safety theory | `stoop-domain-reference` |

## 1. Change classes

Two classes, per root `CLAUDE.md` §Git:

| Class | Path to main | Examples |
|---|---|---|
| **Docs-only** (`docs/`, mockups, strategy) | Push directly to `main` is allowed (`docs: …` commits) | roadmap edits, decision records, spec amendments |
| **App code** (`apps/api`, `apps/web`, `.github`, migrations, anything executable) | ALWAYS branch → PR → CI → review → squash-merge → delete branch. Never commit directly to `main`. | features, fixes, tests, CI config |

CI exists (`.github/workflows/ci.yml`), so the "once CI exists (#14)" clause in root `CLAUDE.md` is satisfied — the PR path is mandatory for all app code. Caveat: a docs edit that changes a spec code depends on (e.g. `schema-v1.md`, `severity-rubric-v1.md`) is not "docs-only" in spirit — see §5 frozen artifacts before touching those.

As of 2026-07-05, GitHub branch protection is NOT platform-enforced (GitHub Pro pending) — the no-push-to-main rule for app code is held by discipline and review only (session-verified 2026-07-05; no repo artifact). Treat it as if enforced.

## 2. The /ship pipeline, step by step

Defined in `.claude/commands/ship.md`. Follow every step in order; never skip or reorder.

### 2.1 Branch

Pattern `<type>/<short-description>`, lowercase, hyphenated, ≤40 chars. Types: `feat/`, `fix/`, `chore/`, `ci/`, `docs/`.

```bash
git checkout main && git pull origin main
git checkout -b feat/approve-endpoint
```

### 2.2 Orient

Identify the app (`apps/api` or `apps/web`); read the issue (`gh issue view <N>`), root `CLAUDE.md`, the app's `CLAUDE.md`, and the docs the issue cites. Scope to exactly what was asked — no bonus refactors.

### 2.3 Build via the crew (see §3)

Implementer/frontend-builder builds; spec-guardian reviews; safety-reviewer and copy-guardian when their triggers fire (matrix in §3). Paste reviewer verdicts into the PR description.

### 2.4 Commit

Conventional-ish subject per root `CLAUDE.md` (`feat(api): …`, `fix(web): …`, `docs: …`), imperative, ≤72 chars. Per `ship.md`:

- **Banned in commit messages**: "Claude", "AI", "Co-authored", "as requested", "Update code to…", trailing periods, filler. No body unless the why is genuinely non-obvious.
- **Stage files explicitly — never `git add .`**:

```bash
git add apps/api/app/routers/drafts.py apps/api/tests/test_drafts.py
git commit -m "feat(api): approve endpoint with 5s undo window"
```

### 2.5 Push + PR

```bash
git push -u origin <branch-name>
gh pr create --repo LaithAlz/stoop-backend --title "<imperative title>" --body "<body>"
```

PR body follows `.github/PULL_REQUEST_TEMPLATE.md`: **What** (one sentence) / **Why** (one sentence, `Closes #N`) / **Test plan** (checklist) / **Notes** (trade-offs, deferred follow-ups). Append the reviewer verdicts (§3).

### 2.6 CI

```bash
gh pr checks <N> --repo LaithAlz/stoop-backend --watch
```

CI (`.github/workflows/ci.yml`, single job `backend (lint, type, test)`) runs, in fail-fast order: `uv sync --frozen` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run mypy app` → `uv run alembic upgrade head` (against a Postgres 15 service) → `uv run pytest -m "not eval"`. Red check → read the log, fix, push, watch again. Never proceed red.

### 2.7 Senior review

Spawn a fresh review agent (frontier model — §4 rule 3) on `gh pr view <N>` + `gh pr diff <N>` covering correctness, security, async correctness, type safety, scope. Output: BLOCKING / ADVISORY / "LGTM — ready to merge". BLOCKING → fix, push, re-run CI, re-review. Repeat until LGTM.

### 2.8 Merge

Only after the §6 hard checks pass:

```bash
gh pr merge <N> --repo LaithAlz/stoop-backend --squash --delete-branch
```

Confirm the merge landed and the branch is gone.

## 3. The dev-agent crew and the mandatory-gate matrix

Source: `docs/03-engineering/dev-agents.md` + `.claude/agents/*.md`. The crew builds the product; the product's own LangGraph agent is a different thing entirely.

| Gate | Agent (`.claude/agents/`) | Model | Writes code? | Fires when |
|---|---|---|---|---|
| Build | `implementer` | Sonnet | Yes | any backend issue |
| Build | `frontend-builder` | Sonnet | Yes | any web UI task |
| Spec review | `spec-guardian` | Sonnet | No (read-only) | EVERY /ship diff, after build, before PR |
| Safety review | `safety-reviewer` | Opus | No (read-only) | see mandatory triggers below |
| Copy review | `copy-guardian` | Haiku | No (read-only) | any customer-visible string added or changed |

**safety-reviewer is MANDATORY** (per `dev-agents.md`) for any change touching:

- **Issues**: #10, #22, #23, #40, #44, #45, #107, #108, #109, #115, #122 (JWT verification, RLS, isolation suite, Twilio webhook, approve/reject flows, Tier-0 prefilter, emergency escalation, degraded mode, approve-by-SMS)
- **Paths** (any file in): `apps/api/app/agent/`, `apps/api/app/routers/webhooks/`, `apps/api/app/deps.py`, `apps/api/app/integrations/supabase_auth.py`

Loop rules and verdicts:

1. spec-guardian returns **APPROVE** or **FIX FIRST** with findings (severity, file:line, spec line violated, minimal fix). Findings go back to the builder — **max 2 fix loops, then escalate to a human**.
2. safety-reviewer attacks the diff (auth confusion, RLS bypass, emergency-path silence, approve replay/race, prompt injection) and ends with the sentence "would you bet a building on this merge?".
3. copy-guardian returns PASS or `{string, file:line, rule broken, suggested rewrite}`.
4. **All verdicts are pasted into the PR description** — the PR is the audit record.

Design principles that never bend: builders and reviewers are different agents (the author never approves its own code); reviewers are read-only so they cannot "fix" their way past a finding; agents stop instead of inventing (missing column name / credential / contract → report and halt).

## 4. The 8 never-break rules — rationale, enforcement, incident

Quoted from root `CLAUDE.md` §"Rules that never bend". Full incident detail lives in `stoop-failure-archaeology`; this table is why each rule exists and what enforces it.

**Rule 1 — "The emergency line is never paywalled, throttled, or gated."**
Rationale: the free emergency line is the product's safety covenant; a tenant in danger must never hit a billing check. Enforcement: no flag or billing read may sit on the emergency path (rule 7 companion); `implementer.md` hard rule bans feature-flag reads in `agent/`, prefilter, and notifications modules; spec-guardian greps for them explicitly. No incident — keep it that way.

**Rule 2 — "`messages` and `audit_log` are append-only. No UPDATE/DELETE, ever, anywhere."**
Rationale: these tables are the dispute-proof record of what a tenant said and what Stoop did; a mutable log is worthless in a conflict. Enforcement: migration `apps/api/migrations/versions/0005_app_role_and_rls.py` executes `REVOKE UPDATE, DELETE ON messages, audit_log, message_status_events FROM app_role` plus blanket REVOKEs from `anon`/`authenticated` — the database itself refuses; code must not fight that. implementer hard rule + spec-guardian check back it up at review time.

**Rule 3 — "Nothing sends to a tenant or vendor without landlord approval", except emergency safety instructions.**
Rationale: Stoop speaks in the landlord's voice; one unapproved message is an irreversible trust and legal event. Auto-send exists only via the trust ladder, only for `routine`, per `(property, severity)`. Enforcement: exactly two code paths may call twilio send — the draft flow and the emergency safety path (`apps/api/CLAUDE.md`); spec-guardian audits twilio-send call sites on every diff.

**Rule 4 — "The rubric is embedded verbatim (`severity-rubric-v1.md` → checksum test). A prompt or rubric change = new version file + full eval run."**
Rationale: silent rubric drift can silently reclassify an emergency as routine; classification behavior must be versioned and eval-gated, never edited in place. Enforcement: `apps/api/tests/test_rubric.py` — a byte-for-byte equality test between `app/agent/rubric.py::RUBRIC_V1` and the ```text block in `docs/02-product/severity-rubric-v1.md`, PLUS a pinned sha256 (`469cd67017c3b5604550c488bb1a6840ebeabb31a55e3a9b5a46a47014d18484`) that catches coordinated-but-subtly-wrong updates to both. Runs on every CI pass. Change procedure: §5.

**Rule 5 — "Never log JWTs, tenant phone numbers, or message bodies" in app logs, Sentry, or error messages.**
Rationale: a secret or PII in a log is unrecoverable once emitted. Incident: the live Twilio auth token was echoed into terminal output by a `source .env` under zsh; the token had to be treated as compromised and rotated in the Twilio console (session-verified 2026-07-05; no repo artifact). Standing rules from that incident: never `source .env`, never `cat`/`echo` secrets, `.env` is gitignored and never read by discovery agents. Enforcement in code: when the webhook must alert on an unknown phone number it logs an HMAC-SHA256 **keyed** digest, never the number (`apps/api/app/routers/webhooks/twilio.py::_digest` — keyed so a raw-hash rainbow lookup over the phone-number space is impossible); Sentry configured with `send_default_pii=False` AND `include_local_variables=False` (`apps/api/app/observability.py` — the former alone still leaks JWTs via stack-frame locals; both marked "NEVER True").

**Rule 6 — "Schema names come from `schema-v1.md`. A new column means editing that doc first, then the migration."**
Rationale: `docs/` is the source of truth — code follows docs, never vice versa; invented names fork the schema silently. Enforcement: spec-guardian check #2 treats any invented table/column name as a CRITICAL finding; implementer is instructed to STOP and report rather than invent.

**Rule 7 — "Analytics discipline (ADR-5)": PostHog identifies by landlord uuid only; session replay off; "Feature flags never gate safety behavior."**
Rationale: flag-service failure must be indistinguishable from flags-off (`apps/api/CLAUDE.md`) — if a flag could gate the emergency path, a flag outage becomes a missed emergency. Enforcement: implementer hard rule (no flag reads in `agent/`, prefilter, notifications) + spec-guardian grep on every diff.

**Rule 8 — Customer-facing copy: plain English (never "triage"), no legal/LTB on marketing pages, never "founding/cohort/spot counts" — say "early access"; exact prices (free Emergency Line / $10 Full Plan / $5 early-access grandfathered / PMs $1.50/door).**
Rationale: public claims must match the ToS and reality; fabricated social proof and unverifiable claims are legal and trust liabilities. Incident: commit `8054652` / PR #142 "remove fabricated testimonials and soften price-lock claim" — testimonials had been invented for the marketing page and had to be stripped. Enforcement: copy-guardian gate on every customer-visible string change; the full rule set (banned words, price strings, grade-5 SMS rules, ToS-claim limits) lives in `.claude/agents/copy-guardian.md` and `docs/02-product/plain-language-rules.md`.

## 5. Founder-elevated hard rules (treat as never-break rules 9–12)

Elevated by the founder on 2026-07-05 (session-verified; the elevation itself has no repo artifact, but each rule's machinery does where noted).

**9. Paid eval runs are founder-gated.** A full eval run hits the real Anthropic API (observed 73.65–84.02¢ per 20-scenario run across gates 7–9, ~15–25 min on a Tier-1 key; the 2026-07-06 gate-9 record — the 20/20 green run — sums to 84.02¢). This paragraph is the ONE home for the cost/duration figures — sibling skills point here instead of restating them; re-verify with `scripts/eval-summary.py` (in `stoop-diagnostics-and-tooling`, prints "total run cost") or by summing per-scenario `cost_cents` in `apps/api/evals/results/last-run.json` after the next approved run. Agents never fire them autonomously; a standing founder directive for a thread of work counts as the go-ahead. Repo backing: `apps/api/evals/runner.py` header states running for real "is the ORCHESTRATOR's call, never something an agent fires autonomously"; `dev-agents.md` lists `pytest -m eval` under human gates; `implementer.md` forbids it outright ("flag when the orchestrator should"). Machinery testing without cost: `EVAL_DRY_RUN=1`. Historical trap, CLOSED by PR #177: `pyproject.toml` now carries `addopts = "-m 'not eval'"`, so bare `uv run pytest` cannot reach the paid gate; the deliberate gate invocation is an explicit `uv run pytest -m eval` (CLI -m overrides addopts, last one wins).

**10. Live Supabase dry-run for any migration touching roles, GRANTs, or RLS — before merge.** Incident: migration 0004 (auth.users trigger) was green on local Docker Postgres and FAILED on live Supabase with "must be able to SET ROLE"; live probing then showed `postgres` on Supabase is not superuser, `pg_has_role(…, 'MEMBER')` is true immediately after `CREATE ROLE` on PG16+, and `GRANT <role> TO CURRENT_USER` as postgres terminates the connection (session-verified 2026-07-05; the shipped fix — a postgres-owned SECURITY DEFINER redesign — is `apps/api/migrations/versions/0004_auth_users_lifecycle_trigger.py`, PR #166). Why the rule: local Docker runs as bootstrap superuser and is structurally blind to privilege bugs. Platform-fact detail: `stoop-domain-reference`; run mechanics: `stoop-run-and-operate`.

**11. Model split.** Frontier model (Opus/Fable-class) plans, orchestrates, and safety-reviews; Sonnet-class implements; Haiku pattern-matches strings. Repo backing: the `model:` frontmatter in `.claude/agents/*.md` (implementer/frontend-builder/spec-guardian = sonnet, safety-reviewer = opus, copy-guardian = haiku) and the "model-to-risk matching" principle in `dev-agents.md` — Opus only where wrong = catastrophic. Never run the safety review on the implementing model, and never let the implementing agent self-review.

**12. One active agent per working tree.** Incident: a frontend agent ran `git checkout`/`git stash` in the main working tree while another agent had uncommitted eval work there — the work was destroyed mid-flight and recovered only via reflog and manual rework (session-verified 2026-07-05; no repo artifact). Parallel repo work uses worktrees: `git worktree add ../LandlordAI-<topic> <branch>` — one agent per tree, no exceptions.

## 6. Frozen artifacts — what "changing" each one requires

"Frozen" = never edited in place. The change procedure for each is a version bump, and every version bump is a founder gate (`dev-agents.md`: prompt/rubric version bumps never delegate).

| Artifact | Frozen by | To change it |
|---|---|---|
| `apps/api/app/agent/rubric.py` + the ```text block in `docs/02-product/severity-rubric-v1.md` (rubric v1.0) | `tests/test_rubric.py`: byte-equality + pinned sha256 `469cd670…d18484`, every CI run | Per `rubric.py`'s own docstring: (1) write `severity-rubric-v2.md`, (2) create `app/agent/rubric_v2.py` verbatim, (3) create `app/agent/prompts/v{n+1}.py` pointing at it — the next FREE version, which is **v3** as of 2026-07-06: `prompts/v2.py` already exists (templates-only bump, commit `11564c8`, merged via PR #177), so `rubric.py`'s docstring literally saying "create prompts/v2.py" is stale — flag it for a repo-side fix in the next PR touching that file, and never create/overwrite an existing version file. (4) full eval run — never skip, (5) update the graph to the new prompt version. Plus new pinned sha in the checksum test. Founder go-ahead required (version bump + paid run). |
| `apps/api/app/agent/prompts/v*.py` — v1 AND v2 | v1 docstring: "FROZEN. Convention: never edit this file." v2 docstring: "FROZEN once merged; to change behavior add v3." (v2 = founder-approved 2026-07-06 templates-only bump, commit `11564c8` + pre-merge amendment `31bd498`, live on `test/eval-harness`; **verified by eval gate 9: 20/20, `release_blocked=False`, 2026-07-06** — baseline snapshot committed as `apps/api/evals/results/v1-baseline.json`, commit `7fe8609`) | Add `prompts/v{n+1}.py` (v3 next) + full eval run + graph update. Founder go-ahead required. The LIVE `REFUSAL_TEMPLATES` are now v2's — `legal_rent_ltb` + `impersonation` rewritten plain-language, `access_codes` + `cost_compensation` plained, `other_tenants` byte-identical to v1; system-prompt builders are re-exported from frozen v1 by construction. Editing templates is a prompt change. Re-verify the exact v2 diff via `apps/api/tests/test_agent_schemas.py::test_prompts_v2_changes_exactly_the_founder_approved_templates`. |
| Eval scenarios: `apps/api/evals/scenarios/*.yaml` (11 LLM scenarios + 9 Tier-0 negatives in `negative_prefilter/`, as of 2026-07-05) + scoring rules in `docs/02-product/eval-scenarios-v1.md` | Convention — changing scenarios changes the definition of passing | Adding scenarios is encouraged (a new production misclassification ⇒ new eval YAML the same week, per `apps/api/CLAUDE.md`). Weakening/removing a scenario or changing scoring = doc amendment + founder gate + full eval run. |
| `apps/api/app/agent/prefilter.py` pattern set (change-disciplined, not frozen) | The #144 discipline (commit `b13a654`) | Additive/monotonic only; zero HARD→silent flips; every pattern change lands with a regression test class in `tests/test_prefilter.py` (41 test classes as of 2026-07-05) and must keep the eval `prefilter_must_fire` cases green. The LLM may escalate past a Tier-0 miss, never de-escalate a Tier-0 fire. |

Note on `apps/api/CLAUDE.md`'s "temperature 0" wording: it predates PR #175. `claude-sonnet-5` rejects the `temperature` parameter (live API 400), so `app/integrations/anthropic.py` deliberately OMITS it and tests assert its absence; determinism is owned by the eval gate (3 samples per classification, flaky = fail). Do not "helpfully" re-add temperature.

## 7. Merge protocol — hard checks before `gh pr merge`

1. **CI green ON THE TIP COMMIT.** Incident: PR #166 was squash-merged while the final commit's CI run was still pending — an earlier commit's green was mistaken for the tip's (session-verified 2026-07-05; no repo artifact). `--watch` alone is not proof; verify the checks ran on the head SHA:

   ```bash
   TIP=$(gh pr view <N> --repo LaithAlz/stoop-backend --json headRefOid -q .headRefOid)
   gh api "repos/LaithAlz/stoop-backend/commits/${TIP}/check-runs" \
     -q '.check_runs[] | .name + " " + .status + " " + (.conclusion // "PENDING")'
   ```

   Every line must read `completed success`. If you pushed after the watch started, re-check.
2. **Senior-review LGTM** (§2.7) with all BLOCKING items resolved, and crew verdicts pasted in the PR description (§3.4).
3. **Never merge red.** A failing check means fix → push → re-run CI → re-review. There is no override path.
4. **Never push app code to main.** Docs-only commits may (§1); everything else goes through the PR.
5. **Squash-merge and delete the branch** — `gh pr merge <N> --repo LaithAlz/stoop-backend --squash --delete-branch` — keeping main history linear (one commit per PR, matching the `git log --oneline` convention visible on main).

## 8. Human-only decisions — never delegate, never attempt

From `dev-agents.md` and `apps/api/CLAUDE.md` §"Things humans must do". If a task needs any of these, stop and say so — do not improvise.

| Decision | Why human-only |
|---|---|
| Merging a PR | Never done by crew subagents; the orchestrator merges only after all §7 checks, under an explicit founder standing directive |
| `pytest -m eval` / any paid eval run | Real API cost; founder-gated (§5 rule 9) |
| Account creation: Supabase, Twilio, LangSmith, Sentry, Fly | Credentials and billing ownership |
| Secrets (`fly secrets set`, env values, token rotation) | Secret custody — see rule 5 incident |
| A2P / CASL filings | Regulatory attestations |
| Stripe dashboard products; any pricing change | Money; rule 8 price strings are downstream of these |
| DNS / domain changes | Production availability |
| Prompt/rubric version bumps | §6 — every one implies a paid eval run and a behavior change to the safety classifier |

## Provenance and maintenance

Volatile claims and their one-line re-verification commands (run from the repo root):

| Claim | Re-verify with |
|---|---|
| safety-reviewer mandatory issue list + path triggers | `grep -n -A3 "safety-reviewer" docs/03-engineering/dev-agents.md` |
| Crew models (sonnet/opus/haiku) | `grep -n "^model:" .claude/agents/*.md` |
| /ship hard rules (no `git add .`, never merge red, banned commit content) | `sed -n '54,68p;134,143p' .claude/commands/ship.md` |
| Pinned rubric sha256 | `grep -n "_PINNED_SHA256" apps/api/tests/test_rubric.py` |
| Rubric change procedure (5 steps) | `sed -n '1,14p' apps/api/app/agent/rubric.py` |
| prompts/v1.py + v2.py frozen conventions and versions | `grep -rn "PROMPT_VERSION" apps/api/app/agent/prompts/*.py; sed -n '1,8p' apps/api/app/agent/prompts/v2.py` |
| v2 diff is exactly the founder-approved template change | `grep -n "test_prompts_v2_changes_exactly_the_founder_approved_templates" apps/api/tests/test_agent_schemas.py` |
| Append-only REVOKE (rule 2) | `grep -n "REVOKE UPDATE, DELETE" apps/api/migrations/versions/0005_app_role_and_rls.py` |
| CI step order and `-m "not eval"` | `grep -n "run:" .github/workflows/ci.yml` |
| PR template sections | `cat .github/PULL_REQUEST_TEMPLATE.md` |
| addopts guard present (bare pytest excludes eval) | `grep -n "addopts" apps/api/pyproject.toml` (expect `-m 'not eval'`) |
| Scenario counts (11 LLM + 9 negatives) | `ls apps/api/evals/scenarios/*.yaml \| wc -l; ls apps/api/evals/scenarios/negative_prefilter/*.yaml \| wc -l` |
| temperature omitted on claude-sonnet-5 | `grep -n "temperature\|^MODEL" apps/api/app/integrations/anthropic.py` |
| Keyed digest instead of phone numbers (rule 5) | `grep -n "_digest" apps/api/app/routers/webhooks/twilio.py` |
| Sentry PII flags both False (rule 5) | `grep -n "send_default_pii\|include_local_variables" apps/api/app/observability.py` |
| Prefilter regression-class count | `grep -c "class Test" apps/api/tests/test_prefilter.py` |
| Rule-8 incident commit (PR #142) | `git log --oneline \| grep "#142"` |
| Branch protection still absent | `gh api repos/LaithAlz/stoop-backend/branches/main/protection` (404 = still absent) |
