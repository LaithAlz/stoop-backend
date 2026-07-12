---
name: stoop-core-loop-campaign
description: >-
  The executable, decision-gated campaign to close Stoop's Train-1 core loop
  (LandlordAI monorepo). Load this when you are about to implement or plan any
  of — issue #34 "wire the StateGraph / Postgres checkpointer", #43 "shadow
  mode / interrupt() before send", #44 "POST /v1/drafts/{id}/approve" or the
  undo window, #45 reject / edit-and-send, #108 emergency protocol / landlord
  escalation chain, #109 degraded mode / classification failure handling,
  #50 end-to-end SMS flow test, #111 per-message cost metering — or when asked
  "close the core loop", "wire the graph", "build the sender", "add the first
  twilio send", "resume the thread after approval", "handle stale drafts on
  approve", "escalation chain sweeper", "holding ack", or "what's left for the
  Train-1 gate". It gives the phase order, exact commands, expected
  observations at every gate, branch-on-symptom triage, mandatory reviewers
  per phase, and the wrong paths that are fenced off.
---

# Stoop core-loop campaign — close the Train-1 loop

Repo: `/Users/laith/Businesses/LandlordAI` (monorepo). Backend: `apps/api`
(Python 3.12 / FastAPI / LangGraph, uv). GitHub: `LaithAlz/stoop-backend`.
Read root `CLAUDE.md` and `apps/api/CLAUDE.md` first — nothing here overrides
them.

**The mission (founder-selected, 2026-07-05):** an inbound tenant SMS today is
persisted, Tier-0-screened, and classified/drafted by standalone nodes — but no
assembled graph runs them, nothing pauses for approval, and **no code anywhere
sends an outbound SMS**. This campaign wires webhook → graph → approval →
send → emergency chain, in five phases, each behind change control. Campaign
order (founder directive): **#34+#43 → #109/#108 → #44/#45 → #50 → #111**
(phases below are numbered by dependency; #109/#108 may land before or after
#44/#45 — Phase 4's sender depends on the send helper introduced in Phase 3,
so if you build Phase 4 first, the send helper moves there and Phase 3 reuses
it; either way the "one send seam" fence holds).

**Campaign status (as of 2026-07-12):** Phase 1 (#34) MERGED — PR #185
(squash `69abba4`). Phase 2 (#43) MERGED — PR #187 (squash `a61b95e`,
2026-07-10; three pre-merge catches recorded as archaeology A23). Phase 4's
**#109 degraded half MERGED** — PR #188 (squash `161e24c`, 2026-07-12;
live DB at migration head **0009**, which adds the `tenant_ack`/
`degraded_retry` notification types). Still open: **#108** (escalation
chain + first sender — in flight), **#44/#45** (Phase 3), **#50**, **#111**.
Standing deployment gap until #108 lands: nothing schedules
`sweep_degraded_mode_retries` and no sender exists, so `tenant_ack`/
`needs_eyes` rows accumulate undelivered — see the DEPLOYMENT-GATING FACT
docstring in `app/agent/degraded_mode_sweep.py` and architecture-contract
weak point 2. Treat the merged phases' sections below as contracts to
preserve, not work to do.

Definitions used throughout:
- **Tier-0 prefilter** — deterministic regex emergency filter
  (`apps/api/app/agent/prefilter.py`), runs in the webhook before any LLM.
- **Case** — one unit of triage work; one LangGraph thread
  (`cases.langgraph_thread_id`, UNIQUE NOT NULL); one approval card.
- **Checkpointer** — LangGraph's Postgres persistence
  (`AsyncPostgresSaver`, tables in the dedicated `langgraph` schema).
- **Stale draft** — a pending draft superseded by a newer tenant message;
  kept in the audit trail, never sendable.
- **Degraded mode** — the LLM-down behavior in
  `docs/02-product/emergency-prefilter.md`: holding ack + landlord
  notification, never silence.

## When NOT to use this skill

| You actually need | Go to |
|---|---|
| How changes ship: gates, reviewer matrix, never-break rules | `stoop-change-control` |
| Symptom-driven triage (checkpointer knobs, flaky 401s, shell traps) | `stoop-debugging-playbook` |
| Full incident history with evidence | `stoop-failure-archaeology` |
| Load-bearing design decisions and invariants | `stoop-architecture-contract` |
| Rubric doctrine, Supabase platform facts | `stoop-domain-reference` |
| Running migrations, live-DB discipline | `stoop-run-and-operate` |
| Test/eval evidence discipline | `stoop-validation-and-qa` |
| Recreating the dev environment | `stoop-build-and-env` |

## Contracts this campaign implements (read before coding)

The campaign implements THESE documents — not your own designs:

1. `docs/02-product/conversation-model.md` — channel vs case, lifecycle,
   the stale-draft rule, approval-queue ordering.
2. `docs/02-product/emergency-prefilter.md` — Tier-0 composition, degraded
   mode, the escalation chain timings, the holding-ack template (verbatim).
3. `docs/03-engineering/api-contracts.md` — draft endpoints, error envelope
   `{"error":{"code","message","request_id"}}`, webhook contracts.
4. `docs/03-engineering/schema-v1.md` — every table/column name. **Never
   invent a column**; edit that doc first, then the migration.
5. GitHub issues carry per-issue acceptance criteria — read each before its
   phase:

```bash
for n in 34 43 44 45 108 109 50 111; do gh issue view $n --repo LaithAlz/stoop-backend; done
```

## Change control (applies to every phase)

- Flow: branch → build → spec-guardian → safety-reviewer → (copy-guardian if
  customer-visible strings) → PR → CI → senior review → squash-merge. Merging
  is a human gate. Details: `stoop-change-control`.
- **safety-reviewer is mandatory for EVERY phase of this campaign**:
  `docs/03-engineering/dev-agents.md` requires it for issues #44 #45 #108
  #109 and for any file in `agent/`, `webhooks/`, `deps.py`,
  `integrations/supabase_auth.py` — every phase touches at least one.
- Eval gate re-runs **only** when a phase touches `app/agent/prompts/`,
  `app/agent/rubric.py`, `app/agent/prefilter.py`, or `evals/` — and then it
  is a new prompt/prefilter version + full paid run + **founder go-ahead**
  (paid runs are never fired autonomously; current cost/time figures live in
  `stoop-change-control` rule 9). Phases 1–3 should touch none of those.
- Before merge: `gh pr checks <N> --repo LaithAlz/stoop-backend --watch` AND
  confirm the check run's headSha equals the branch tip (a PR was once merged
  on a stale commit's green — session-verified 2026-07-05; no repo artifact).
- One active agent per working tree; parallel work uses `git worktree add`
  (a parallel agent once destroyed uncommitted work with `git checkout` in a
  shared tree — session-verified 2026-07-05; no repo artifact).

Safe test invocation (three shell traps, all recurring — session-verified
2026-07-05; no repo artifact): bare `uv run pytest` collects the **paid**
`eval` marker; a missing `DATABASE_URL` makes integration tests hit a
placeholder URL and spray connection errors that look like real breakage;
piping to `tail` masks the exit code. Always:

```bash
cd apps/api && export DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop' && uv run pytest -m "not eval" -q; echo EXIT=$?
```

(Local Postgres: `docker compose up -d` from the repo root — service
`postgres`, db/user/pass all `stoop`.)

---

## PHASE 0 — Preconditions (do not start Phase 1 until all green)

1. **Eval-harness PR merged.** The eval harness lives on branch
   `test/eval-harness` — MERGED as PR #177, squash `3ddd15e` (7 commits: `f38d0f0`, `1d2c793`,
   `e9ce472`, `33441d2`, `11564c8`, `31bd498`, `7fe8609` — as of 2026-07-06;
   merged as PR #177, 2026-07-06). It must be PR'd and merged
   with the eval gate green before graph work begins (the graph's quality
   floor IS the eval gate). Gate history as of 2026-07-06: gate 6 = 19/20
   (f1-rent-ltb hard-failed); gate 7 (post-`33441d2` refusal-ack fix) =
   18/20 — f1 hard-failed AGAIN, root-caused to the v1 REFUSAL_TEMPLATES' own
   legalistic copy (the u1 soft-fail was one-off draft nondeterminism); the
   founder approved a templates-only **prompts v2** bump (`11564c8`;
   consumers import `prompts.v2`, drafts/audit stamp `prompt_version="v2"`);
   gate 8 — the first v2 run — = 18/20, f1 hard-failing on the residual
   relative-time word "soon" plus an e4 INFRA schema variance, both fixed by
   `31bd498`; **gate 9 (`last-run.json` generated 2026-07-06T06:43Z) =
   20/20, `release_blocked=False` — THE GATE IS GREEN**, baseline snapshot
   committed as `apps/api/evals/results/v1-baseline.json` (`7fe8609`).
   **Always re-check the file; never trust this paragraph's snapshot.**

   ```bash
   cd apps/api && python3 -c "import json; d=json.load(open('evals/results/last-run.json')); print(d['generated_at']); print(d['summary'])"
   ```

   Gate is green iff `summary.release_blocked` is `false` and
   `hard_failed_scenario_ids` is empty. If not green → the fix loop belongs
   to the eval thread, not this campaign; a new paid verification run needs
   the founder's go-ahead. Harness machinery can be exercised free with
   `EVAL_DRY_RUN=1` (see `apps/api/evals/runner.py`).

2. **docs/engineering-decisions merged AFTER the eval PR** (branch exists,
   pushed, unmerged; its §8 must cite the eval PR number — session-verified
   2026-07-05; no repo artifact).

3. **State verification** (run all; expected on the right):

   ```bash
   git -C /Users/laith/Businesses/LandlordAI fetch --all --quiet && git -C /Users/laith/Businesses/LandlordAI log --oneline -1 origin/main   # contains the eval-harness merge
   gh pr list --repo LaithAlz/stoop-backend --state open                     # no stale campaign PRs
   git -C /Users/laith/Businesses/LandlordAI status --short                  # clean tree (or your own branch only)
   cd apps/api && uv run alembic upgrade head && uv run alembic current      # head = 0009 (as of 2026-07-12)
   cd apps/api && uv run ruff check . && uv run mypy app                     # clean baseline
   ```

   If `alembic current` is not `0009` → run against local Docker Postgres
   only; never point `DATABASE_URL` at the live pooler for dev work
   (`stoop-run-and-operate`).

---

## PHASE 1 — #34: wire the StateGraph with the Postgres checkpointer

**STATUS: MERGED — PR #185 (squash `69abba4`).** The gates G1–G3 below are
now invariants to preserve, not work to do.

**What exists (all on main after Phase 0):** six node functions, each
`async def node(state: AgentState) -> dict` — `identify_property`,
`load_context`, `identify_case`, `classify_intent`, `classify_severity`,
`draft_response` in `apps/api/app/agent/nodes/`; the state TypedDict
(`app/agent/state.py`); the checkpointer module (`app/agent/checkpointer.py`,
already `setup_checkpointer()`'d in `app/main.py`'s lifespan after
`verify_request_engine_role_separation()`); and the honest stub
`app/agent/graph_entry.py::enqueue_classification` that the webhook schedules
as a background task — **this stub's body is what #34 replaces.**

**Build steps:**

1. Assemble the graph in `app/agent/graph.py` (the target-layout home per
   `apps/api/CLAUDE.md`). Node order per issue #34's AC:
   `identify_property → load_context → identify_case → classify_intent →
   classify_severity → branch(emergency_protocol | draft_response →
   interrupt) → send+log+trust`. Known tension to resolve with
   spec-guardian, not by inventing: `state.py` says `identify_case` may
   eventually consume an intent signal, while today
   `app/agent/nodes/identify_case.py` treats that signal as an always-`None`
   seam — the issue's order works with today's code; reordering is a spec
   question.
2. Checkpointer wiring: `get_checkpointer()` per invocation (cheap,
   documented safe), compile with it, and invoke with
   `{"configurable": {"thread_id": case.langgraph_thread_id}}` — **one
   thread per case, never per tenant/phone** (`app/agent/checkpointer.py`
   docstring, schema-v1.md v1.4 notes). Design decision you must make
   explicitly (flag it in the PR): the thread is per case but the case is
   only known after `identify_case` runs — whatever structure you choose
   (pre-routing segment vs. full-graph), the checkpoint thread that persists
   MUST be keyed on `cases.langgraph_thread_id`, and resume-from-checkpoint
   after process restart is an acceptance criterion.
3. `reasoning_log` accumulation — pick ONE, never mix:
   - (a) keep LangGraph's default last-write-wins + the existing defensive
     convention (every node reads the full incoming log, appends, returns
     the full list) — zero node changes; or
   - (b) add a reducer (`Annotated[list[str], operator.add]`) AND change
     every node to return only its new lines. Despite `state.py`'s
     optimistic comment, mixing (b) with full-list returns duplicates every
     prior line on every node — write the regression test either way: run
     two nodes, assert no duplicated log lines.
4. Replace `enqueue_classification`'s stub body with the graph invocation.
   Keep its properties: opens its own admin session, never raises outward,
   idempotent `message_received` audit guard. Its known accepted race
   (check-then-insert) becomes live once retries exist — fix it here with
   the atomic `WHERE NOT EXISTS` pattern the webhook router already uses
   (the stub's docstring says exactly this).

**Merge-blocking gates — pinned by senior review (session-verified
2026-07-05; no repo artifact):**

| Gate | Requirement |
|---|---|
| G1 | `classification_failed=True` AND `draft_guard_failed=True` must each route to an explicit degraded-mode edge — #109's entry point. At this phase that edge must, at minimum, durably insert a `needs_eyes` notification row and a `degraded_mode` audit row (the tenant-facing holding ack sends once the safety-path sender exists, Phase 4). **Silently ending the graph on either flag is merge-blocking.** |
| G2 | The draft INSERT must survive `uq_drafts_one_pending` (partial unique index, `drafts` WHERE status='pending') via the stale-then-insert pattern that **already exists inside `app/agent/nodes/draft_response.py`** (mark old pending draft `stale` + `draft_stale` audit row, then insert, one transaction). Do not replace it with a naive INSERT. |
| G3 | `identify_case` consumes `state["open_cases"]` loaded by `load_context` — do not re-query open cases in a second node. |

**Expected observations (the gate):** with the Anthropic client mocked, POST
a signed fake webhook (see `tests/test_webhooks_twilio_sms.py` for the
signature fixture pattern) or invoke the graph directly on a seeded message,
then:

```bash
docker compose exec postgres psql -U stoop -d stoop -c "SELECT status, prompt_version FROM drafts ORDER BY created_at DESC LIMIT 1;"          # pending
docker compose exec postgres psql -U stoop -d stoop -c "SELECT action FROM audit_log ORDER BY id DESC LIMIT 5;"                              # includes classified, drafted
docker compose exec postgres psql -U stoop -d stoop -c "SELECT count(*) FROM langgraph.checkpoints;"                                          # > 0
```

Integration tests must prove all three plus resume-after-restart
(re-instantiate the graph, same `thread_id`, state is restored).

**If you see X instead → Y:**
- `psycopg_pool.PoolClosed: ... is not open yet` → `setup_checkpointer()`
  didn't run first; ordering contract in `app/agent/checkpointer.py`
  (`get_checkpointer` docstring). In tests, the conftest autouse reset
  closes the pool per test.
- Checkpointer setup fails at startup / `CREATE INDEX CONCURRENTLY cannot
  run inside a transaction block` / duplicate-prepared-statement errors →
  psycopg3 knob section of `stoop-debugging-playbook`: `autocommit=True`,
  `prepare_threshold=None` (`None`, NOT `0` — `0` means prepare-everything),
  `search_path` pinned twice. Never remove those knobs.
- Dozens of connection errors in integration tests → you forgot
  `DATABASE_URL` in the SAME shell invocation (Phase-0 command above).
- `uq_drafts_one_pending` violation in logs → G2 was bypassed somewhere;
  find the second INSERT path, don't relax the index.

**Change control:** spec-guardian + safety-reviewer (agent/ files). No
prompt/rubric/prefilter touch → no eval re-run. **Rollback:** revert the PR;
checkpoint rows in the `langgraph` schema are additive and harmless; drafts/
audit rows are data and stay (audit_log is append-only — never delete).

---

## PHASE 2 — #43: shadow mode — interrupt() before any send

**STATUS: MERGED — PR #187 (squash `a61b95e`, 2026-07-10)**, after three
pre-merge catches (archaeology A23: dead-code drain from a flawed probe,
TOCTOU resume, stuck-draft crash window). Load-bearing contract learned
there, pinned in `app/agent/nodes/await_approval.py` and on issue #44: the
whole node re-executes on every resume; nothing before a raising
`interrupt()` commits; plain `ainvoke` on a paused thread RESTARTS from
START — so **#44's send must be a SEPARATE node behind a conditional edge,
never code after `interrupt()`**.

Shadow mode = the graph drafts everything but can send nothing.

1. Add `interrupt()` (from `langgraph.types`, verified importable with
   installed langgraph 1.2.7) after `draft_response`, before the (not yet
   existing) send node. Urgent/routine drafts stop there. The emergency
   branch is exempt (safety SMS is Phase 4's sender; there is no approval
   gate on it, ever).
2. Case status: move the case to `awaiting_approval` when the draft parks at
   the interrupt. `draft_response` deliberately does NOT touch
   `cases.status` (its docstring flags this) — #43 owns that transition.
   Note the vocabulary split: `cases.status` has `awaiting_approval`;
   `drafts.status` does not (draft stays `pending`).
3. Stale-draft rule (conversation-model.md, verbatim): new inbound on a case
   with a pending draft → draft marked `stale`, graph re-runs from
   `load_context` with full history, one card per case, freshest draft only.
   The stale-then-insert already handles the draft rows; #43 adds the
   re-run trigger for pending-case inbounds.
4. Approval resumes the thread: `Command(resume=...)` (from
   `langgraph.types`) invoked with the case's `thread_id`. In this phase
   only tests resume; the API endpoint is Phase 3.

**Expected observations:** run the Phase-1 flow → graph pauses;
`cases.status = 'awaiting_approval'`; exactly one `pending` draft per case;
send a second inbound → old draft `stale`, new `pending` draft, `draft_stale`
audit row, still exactly one card's worth of data. Nothing sends —
structurally guaranteed (no send code exists yet). The dashboard `/v1/queue`
endpoint is #56 (Train 2); until then "queue shows the draft" is verified at
the DB level:

```bash
docker compose exec postgres psql -U stoop -d stoop -c "SELECT c.status, d.status FROM cases c JOIN drafts d ON d.case_id=c.id ORDER BY d.created_at DESC LIMIT 2;"
```

**If instead:** two `pending` drafts for one case → the unique index would
have raised; if it didn't, you're on the wrong database. Case auto-staled
while awaiting approval → forbidden: `awaiting_approval` is excluded from
auto-stale (`app/agent/case_lifecycle.py`, conversation-model.md).

**Change control:** spec-guardian + safety-reviewer. **Rollback:** revert;
interrupted checkpoints are inert data.

---

## PHASE 3 — #44/#45: approve / reject / edit-and-send + the sender

**This phase introduces the first outbound-send call site in the codebase.**
Today `app/integrations/twilio.py` contains ONLY signature verification —
there is no send function anywhere (verified by grep, 2026-07-05). The send
helper you add must remain reachable from exactly two flows forever: the
draft sender (here) and the emergency safety path (Phase 4).
safety-reviewer is MANDATORY (dev-agents matrix names #44/#45 explicitly).

**Endpoints — implement `docs/03-engineering/api-contracts.md` §Drafts
exactly:**

| Call | Contract |
|---|---|
| `POST /v1/drafts/{id}/approve` | 200 `{"status":"approved","scheduled_send_at":"…(+5s)","undo_until":"…"}`. Sets `drafts.scheduled_send_at = now()+5s` — **the undo window is data, not a sleep** (schema-v1.md implementer notes). Idempotent on repeat (no double-send). |
| — stale id | 409, code `draft_stale`, body includes `"fresh_draft_id"`, standard error envelope. |
| `DELETE /v1/drafts/{id}/approve` | Within window: 200 `{"status":"pending"}` (undo is just data). After send: 409 `already_sent`. |
| `POST /v1/drafts/{id}/reject` | Body `{"note?"}` → 200; draft archived (`rejected`), case stays open, audit row. |
| `POST /v1/drafts/{id}/edit-and-send` | Body `{"body"}` → same response as approve; original `body` kept, edited text in `drafts.final_body`, `edited=true`; trust metrics count it as a non-clean approval. |
| Race: approve vs new inbound | **Staleness wins** (conversation-model.md edge case): the approve carries the draft id; if stale by then, reject with `draft_stale` + fresh draft. |

On send: resume the graph thread from its checkpoint, write the outbound
`messages` row with `twilio_sid` (delivery status then flows in via the
existing `/webhooks/twilio/status` → `message_status_events`), set
`drafts.sent_message_id`, update `trust_metrics` (clean vs edited), append
`approved` + `sent` audit rows.

**The sender — design menu, ranked (decide in the PR description):**

- **(a) In-process asyncio periodic task — RECOMMENDED for v1.** No new
  infra; started/stopped in the FastAPI lifespan (which already manages the
  checkpointer; note no periodic loop exists in `app/main.py` yet — this is
  the first, and it can also host `case_lifecycle.sweep_cases`, currently
  called only by tests). Obligations you accept: **crash-safety** —
  `approved` rows with a due `scheduled_send_at` survive restart and send
  then; **idempotency** — claim atomically before sending:
  `UPDATE drafts SET status='sending' WHERE id=:id AND status='approved' AND
  scheduled_send_at <= now() RETURNING id` (the `sending` status exists in
  the schema CHECK for exactly this), record the returned `twilio_sid`, then
  flip to `sent`. Exactly-once *attempt* per claim; a crash between claim
  and Twilio-ack is surfaced as a stuck `sending` row, never a silent
  double-send.
- **(b) External cron hitting an internal endpoint.** Obligations: endpoint
  auth + single-flight (two overlapping ticks must not double-claim — the
  same atomic UPDATE covers it). Only if (a) is rejected in review.
- **(c) Queue infrastructure — REJECTED for v1.** ADR-2
  (`docs/03-engineering/architecture.md`): no jobs/queue at v1; a durable
  queue is added when the §11 scaling trigger fires (observed webhook
  retries / background-task losses), on evidence, not faith.

**Expected numbers (test with a fake Twilio client):**
- approve → row flips `approved`, `scheduled_send_at ≈ now()+5s` → after 5 s
  the fake records **exactly one** send; audit has `approved` then `sent`.
- `DELETE .../approve` within 5 s → **zero** sends, draft back to `pending`.
- double-approve → one send. Approve a stale id → 409 + `fresh_draft_id`,
  zero sends.
- restart the app between approve and due-time → the send still happens
  once.

**If instead:** two sends recorded → your claim UPDATE isn't the row's
single-flight gate; fix the claim, do not add a dedupe cache. Send fired
before 5 s → you slept instead of comparing `scheduled_send_at <= now()`.

**Change control:** spec-guardian + safety-reviewer (mandatory) +
copy-guardian if any tenant-visible send/undo strings are added; endpoint
changes update `api-contracts.md` in the same PR. **Rollback:** revert the
PR; `approved`-but-unsent rows become inert (no sender exists after revert)
— note them in the PR so an operator can reject them; never UPDATE audit
rows.

---

## PHASE 4 — #108 emergency executor + #109 degraded mode

**STATUS (2026-07-12): the #109 degraded half is MERGED — PR #188 (squash
`161e24c`; migration 0009). #108 is still open (in flight).** What #188
shipped: the `degraded_mode` graph node (holding-ack `tenant_ack` intents,
blind `needs_eyes` escalation), the 1/5/15-min re-classification retry
sweep (`app/agent/degraded_mode_sweep.py`) with per-exception Sentry paging
+ a bounded exception counter that force-escalates (A24), and `degraded_mode`
audit rows. Explicitly OUT of #188's scope, still owed by #108/deployment:
the cron/scheduler that calls the sweep, and the sender that actually
delivers `tenant_ack`/`needs_eyes` — until both exist the no-keyword leg's
invariant is provisional (the sweep module's DEPLOYMENT-GATING FACT
docstring). The #109 subsection below is the plan that was implemented —
verify against the merged code, and treat the #108 subsection as the
remaining work.

**Seams already built for you (do not re-plumb the webhook):**
- `app/agent/emergency.py::fire_emergency_protocol` — the single execution
  seam; today logs only. The webhook has ALREADY written the durable
  artifacts before calling it: an `audit_log` row
  (`actor='prefilter'`, `action='emergency_triggered'`) and a
  `notifications` row (`type='emergency_call'`, `status='pending'`),
  deduped cross-process by `uq_notifications_message_dedupe` (migration
  0006) — a concurrency race once produced 28/30 duplicate escalations
  before that index existed (session-verified 2026-07-05; no repo
  artifact). **`emergency_call`/`needs_eyes` rows are NEVER deleted** — they
  anchor the dedupe.
- `classify_severity` sets `classification_failed`; Phase 1's G1 edge
  routes it. #109 fills that edge's body.

**#108 — the escalation chain**, per `emergency-prefilter.md` (timings
configurable, defaults verbatim):

| T | Action |
|---|---|
| T+0 | Twilio voice call to landlord — spoken summary + "press 1 to acknowledge" (single TwiML app; `POST /webhooks/twilio/voice`, `Digits=1` → ack). Tenant safety SMS already sent at T+0: category-templated, 911-first for fire/medical/crime, plain-language-rules.md compliant (max 3 numbered steps, ≤15-word sentences, concrete locations). |
| T+2m | unacked → SMS to landlord with ack link |
| T+5m | second voice call |
| T+10m | backup contact (per property, optional): call + SMS |
| T+15m | third call + honest tenant status ("Still reaching ⟨name⟩ — if the situation is getting dangerous, call 911.") |
| T+20m+ | repeat landlord+backup cycle every 15 min until acknowledged |

Drive it as a **state machine on the `notifications` table**: columns
`status` (`pending→sent→acknowledged` / `failed` / `exhausted`), `attempt`,
`next_attempt_at` (the sweeper key; `idx_notifications_sweep` exists),
`acknowledged_at`. A 60-second sweeper tick claims due rows
(same atomic-UPDATE single-flight discipline as Phase 3's sender) — durable
rows mean the chain survives restarts by construction. Acknowledgment =
keypress, tokenized SMS link (`/ack/{token}` → `POST
/v1/notifications/{id}/ack`), or opening the case; it stamps
`acknowledged_at` and stops the chain. Every attempt →
`emergency_call_attempt` audit row; ack → `acknowledged`; "median time to
acknowledgment" must be answerable by one query.

**#109 — degraded mode.** The invariant: **no tenant message ever sits
unacknowledged because an API was down.** 20 s end-to-end budget + one retry
(already enforced in `app/integrations/anthropic.py`: 20 s shared deadline,
12 s first-attempt cap, 2 s retry floor); on failure:

| Condition | Behavior |
|---|---|
| Tier-0 HARD already hit | already handled — protocol fired without the LLM |
| SOFT annotation present | immediate `needs_eyes` notification with the raw text **in the notification payload** (DB rows may carry it; app logs and Sentry NEVER may — never-break rule #5) + holding ack to tenant |
| No keywords | holding ack; re-classify at 1/5/15 min; still failing at 15 min → `needs_eyes` anyway |

Holding ack — template, no LLM, the SHIPPED copy (constant
`app/agent/nodes/degraded_mode.py::HOLDING_ACK_TEMPLATE`, matching
`emergency-prefilter.md`'s corrected template):
"Got your message — it's been passed to ⟨landlord first name⟩. If this is
a life-threatening emergency, call 911."
(The doc's earlier draft ended "…and you'll hear back soon" — the "soon"
clause was REMOVED by copy-guardian ruling in the #109 review round:
plain-language rule 4, never "soon", applies with extra force in the one
message sent while classification is down. See archaeology A24 — do not
quote the old wording back into existence.)
All degraded events → `degraded_mode` audit rows + a Sentry alert
(metadata only). Required chaos test: mock classification to fail → observe
holding ack sent (fake Twilio), `needs_eyes` row, audit rows, Sentry event.

**Expected observations:** HARD-keyword inbound with fakes → T+0 tenant
safety SMS + landlord voice call attempted; advance the clock (inject `now`)
→ T+2m SMS, T+5m call, in order; simulate `Digits=1` → chain stops, later
sweeps do nothing; duplicate webhook replay → zero new `emergency_call`
rows. Degraded chaos test → all rows above.

**If instead:** duplicate escalations under replay → you bypassed the
`ON CONFLICT ... RETURNING`-gated insert in the webhook; side-effects must
be gated on the RETURNING row. Chain lost on restart → you kept schedule
state in memory; move it into `next_attempt_at`.

**Fences specific to this phase:** the emergency safety path is the ONLY
sender besides the draft flow; it is **never feature-flagged, never
paywalled, never throttled** (CLAUDE.md rules 1/3/7 — flags never gate
safety; `agent/` modules read no flags at all, per `emergency.py`'s own
docstring). If you are tempted to touch `prefilter.py` patterns while here:
that is additive-only under the #144 discipline, needs a regression test
class in `tests/test_prefilter.py`, a version note, a full eval run, and the
founder gate — see `stoop-change-control`.

**Change control:** spec-guardian + safety-reviewer (mandatory: #108/#109
named in the matrix) + copy-guardian (safety SMS / holding-ack strings are
customer-facing; plain-language-rules.md applies). Eval re-run only if
prefilter/prompts touched. **Rollback:** revert; pending notification rows
go unswept (harmless); never delete `emergency_call`/`needs_eyes` rows.

---

## PHASE 5 — #50 end-to-end test + #111 cost metering

**#50 — the M1 gate rehearsal.** Build the fake-based end-to-end first
(CI-runnable, no credentials): signed webhook POST → graph (mocked
Anthropic) → draft parks → `POST /v1/drafts/{id}/approve` → fake Twilio
records the outbound; emergency variant: HARD-keyword SMS → landlord call +
tenant safety SMS fired, no approval wait; assert the full audit sequence
for both variants. The issue's staging variant (real Twilio + real API +
LangSmith trace) is credential-gated (Twilio A2P, LangSmith key, Fly deploy
are user-blocked externals — session-verified 2026-07-05; no repo artifact):
build the test so the fakes swap for real clients via env, then stop and
ask.

**#111 — cost metering.** The helper exists:
`app/integrations/anthropic.py::estimate_cost_cents` (placeholder $3/$15 per
MTok table — conservative, in-code). `draft_response` already writes
`tokens_in/out`, `model`, `cost_cents` into its `drafted` audit payload, and
the eval runner already reports per-scenario `cost_cents` in
`last-run.json`. Remaining work: record the same for every Anthropic call
site onto the message/case record, Twilio per-segment cost on outbound
sends, and a cost-per-case / per-door / per-month query or view. **If any of
that needs a new column, `schema-v1.md` changes first** (rule 6) — do not
bolt columns onto `messages` in a migration-first commit.

**Change control:** spec-guardian + safety-reviewer (agent/webhook files in
the e2e fixtures). **Rollback:** tests and read-side metering are
low-risk; a metering view is DROP-able.

---

## Wrong paths — fenced (do not argue with these in-PR)

| Fenced path | Why |
|---|---|
| A second `twilio.send` call site (a "quick notification" here, a "test ping" there) | `apps/api/CLAUDE.md`: sends happen ONLY via the draft flow or the emergency safety path. Every extra site is an unapproved-send hole and breaks never-break rule #3. |
| Gating any safety behavior behind a feature flag | CLAUDE.md rule 7: flags never gate the emergency path, rubric, or approval requirements. Flag-service failure must be indistinguishable from flags-off. |
| Letting the agent de-escalate a Tier-0 fire | `emergency-prefilter.md`: the call already happened; that's the bias rule working. The LLM may escalate a miss, never suppress a fire. Log the disagreement — it's a guard candidate or an eval case. |
| Editing `app/agent/prompts/v1.py`/`v2.py` or `rubric.py` in place | Prompts are frozen per version; the rubric is byte-identical to the doc (checksum test). Change = new version file + full eval run + founder gate. |
| UPDATE on `messages`/`audit_log`/`message_status_events` to track status | Append-only (rule #2; migrations revoke the grants). Append events instead — `message_status_events` exists precisely because delivery status must never UPDATE `messages`. |
| One shared webhook transaction wrapping persist + processing | Caused silent message loss pre-merge (a swallowed exception aborted the transaction; Twilio got 200 and never retried — session-verified 2026-07-05; no repo artifact). Contract: commit the message FIRST; artifact failures 5xx so Twilio retries (`app/routers/webhooks/twilio.py` docstring). |
| Skipping the live Supabase dry-run when a migration touches roles/grants/RLS | Local Docker runs as superuser and is blind to privilege bugs; migration 0004's original design passed locally and failed live (founder-elevated rule; see `stoop-change-control` / `stoop-failure-archaeology`). |
| Running `pytest -m eval` or `python -m evals.runner` (without `EVAL_DRY_RUN=1`) autonomously | Paid; founder go-ahead required, orchestrator-only. |
| Deleting `emergency_call`/`needs_eyes` notification rows in any cleanup/rollback | They are the duplicate-escalation dedupe anchor (migration 0006, schema-v1.md v1.3 note). |

---

## The Train-1 gate — definition of done (measurable)

From `docs/04-roadmap/release-train.md`: **"real tenant texts (incl. a
photo) handled end-to-end; 10 evals green; zero missed emergencies."**

| Criterion | How it's proven |
|---|---|
| Real tenant text handled e2e | #50's staging variant passing (fake-based rehearsal green in CI first) |
| Photo handled | #46 (MMS → Supabase Storage) — tracked separately, after this campaign per founder ordering |
| 10 evals green in CI | #73: CI runs `pytest -m eval` on PRs touching `app/agent/prompts/`, `app/agent/rubric.py`, or `evals/`; E/F-class failures block merge |
| Zero missed emergencies | Every eval E-scenario green + the #50 emergency variant + no `emergency_triggered`-absent HARD-hit in any test corpus; the standing frontier metric (see `stoop-research-and-frontier`) |

**Campaign self-check (run when you believe you're done):**

```bash
for n in 34 43 44 45 108 109 50 111; do gh issue view $n --repo LaithAlz/stoop-backend --json number,state -q '"\(.number) \(.state)"'; done   # all CLOSED
cd apps/api && python3 -c "import json; d=json.load(open('evals/results/last-run.json')); s=d['summary']; assert not s['release_blocked'] and not s['hard_failed_scenario_ids'], s; print('eval gate green:', s['passed'], '/', s['total'])"
cd apps/api && export DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop' && uv run pytest -m "not eval" -q; echo EXIT=$?
grep -rn "messages.create\|<the send helper name you introduced>" apps/api/app --include='*.py' | grep -v test   # exactly the two sanctioned call paths
```

---

## Provenance and maintenance

Volatile claims and their one-line re-verification commands:

| Claim (as of 2026-07-05/06 unless dated otherwise) | Re-verify with |
|---|---|
| Issue states — #34/#43/#109 closed via PRs #185/#187/#188; #44/#45/#50/#108/#111 remaining (as of 2026-07-12) | `for n in 34 43 44 45 108 109 50 111; do gh issue view $n --repo LaithAlz/stoop-backend --json state,title -q '"\(.title): \(.state)"'; done` |
| Shipped holding-ack copy (no "soon") + sweep still uncalled outside tests | `grep -n -A2 "HOLDING_ACK_TEMPLATE = " apps/api/app/agent/nodes/degraded_mode.py; grep -rn "sweep_degraded_mode_retries" apps/api/app --include='*.py' \| grep -v degraded_mode_sweep` |
| Eval gate GREEN (gate 9: 20/20, release_blocked=False, as of 2026-07-06) | `cd apps/api && python3 -c "import json; print(json.load(open('evals/results/last-run.json'))['summary'])"` |
| Eval-harness work merged (PR #177, squash `3ddd15e`, 2026-07-06) | `git log --oneline -3 main` shows the #177 squash; `gh pr view 177 --repo LaithAlz/stoop-backend --json state` |
| `graph_entry.enqueue_classification` still the stub #34 replaces | `grep -n "stub" apps/api/app/agent/graph_entry.py` |
| `fire_emergency_protocol` still the #108 no-op seam | `grep -n "no-op\|#108" apps/api/app/agent/emergency.py` |
| No outbound-send call site exists anywhere | `grep -rn "messages.create\|send_sms" apps/api/app --include='*.py' \| grep -vi anthropic` |
| Migrations head = 0009 (as of 2026-07-12) | `ls apps/api/migrations/versions/ \| tail -3` |
| `uq_drafts_one_pending` / `uq_notifications_message_dedupe` shapes | `grep -n "uq_drafts_one_pending\|uq_notifications_message_dedupe" docs/03-engineering/schema-v1.md` |
| langgraph 1.2.7 / checkpoint-postgres 3.1.0 installed; `interrupt`/`Command` importable | `cd apps/api && uv run python -c "from langgraph.types import interrupt, Command; import importlib.metadata as m; print(m.version('langgraph'))"` |
| safety-reviewer matrix covers #44/#45/#108/#109 + agent//webhooks/ | `grep -n "safety-reviewer" -A 3 docs/03-engineering/dev-agents.md` |
| Escalation timings 0/2/5/10/15 min, 20 s budget, holding-ack wording | `grep -n "T+\|20 seconds\|Got your message" docs/02-product/emergency-prefilter.md` |
| `/v1/queue` still unbuilt (#56, Train 2) | `gh issue view 56 --repo LaithAlz/stoop-backend --json state,milestone -q '"\(.state) \(.milestone.title)"'` |
| Founder campaign ordering + paid-eval founder gate | session-verified 2026-07-05 (no repo artifact) — confirm with the founder if stale |
