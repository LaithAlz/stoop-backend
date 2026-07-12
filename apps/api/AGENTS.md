# apps/api — backend addendum

Adds to the root `AGENTS.md` (all rules there apply). Also read
`apps/api/CLAUDE.md` — binding conventions (contracts, DB, auth, logging,
typing). Facts here stamped as of 2026-07-12.

## Commands

```bash
cd apps/api
uv sync                                   # deps (installs Python 3.12 if missing)
uv run uvicorn app.main:app --reload      # run; needs .env (cwd-relative autoload)
# ^ with a real ANTHROPIC_API_KEY in .env, every inbound webhook message now
#   triggers a real PAID LLM classification (graph wired since PR #185) —
#   use a fake key or a mocked Anthropic client for local end-to-end poking.
uv run alembic upgrade head               # migrate — head is 0009 (as of 2026-07-12)
uv run ruff check . && uv run ruff format --check . && uv run mypy app   # must be green pre-commit
```

Tests: use the root guide's one-shot invocation (same-shell `DATABASE_URL`
export, `-m "not eval"`, `; echo EXIT=$?`). Docker Postgres comes from the
**repo-root** `docker-compose.yml`. Eval harness dry-run (free, zero API
calls): `EVAL_DRY_RUN=1 uv run python -m evals.runner` — anything without
that env var is a paid, founder-gated run. Alembic's URL comes from app
config / `DATABASE_URL`, not `alembic.ini`; migrations must round-trip
(down/up).

## Agent rules digest (the load-bearing ones)

- **Rubric checksum:** `app/agent/rubric.py` is byte-identical to the rubric
  block in `docs/02-product/severity-rubric-v1.md`; `tests/test_rubric.py`
  enforces equality + a pinned sha256 on every CI run. Never edit either side
  alone; behavior change = new version pair + full eval run.
- **Prompts frozen:** `app/agent/prompts/v1.py` (frozen history) and `v2.py`
  (live) are never edited. The next free version is **v3**. A template edit
  IS a prompt change.
- **Tier-0 clamp:** the LLM may escalate past a Tier-0 miss; it may NEVER
  de-escalate a Tier-0 fire (`app/agent/nodes/classify_severity.py` clamps
  from the durable `messages.prefilter` snapshot). Prefilter pattern changes
  are additive-only with a regression test class each.
- **One pending draft per case** — partial unique index
  `uq_drafts_one_pending`; new inbound marks the old draft `stale` and
  re-runs from `load_context`. Use the existing stale-then-insert pattern in
  `app/agent/nodes/draft_response.py`, never a naive INSERT.
- **Two sanctioned Twilio egress routes, forever:** the draft-approval flow
  and the emergency safety path. As of 2026-07-12 NO send function exists at
  all (`app/integrations/twilio.py` is signature-verification only; #108 is
  in flight) — do not add one outside those two flows.
- **Never invent a severity on failure:** classification failure routes to
  the degraded-mode node (holding ack + escalation, built in #188), never to
  a guessed severity. The undo window and all timers are **data**
  (`scheduled_send_at`, `next_attempt_at`), never in-process sleeps.
- Never `await session.commit()` mid-handler after `require_landlord` — it
  kills the `SET LOCAL` RLS GUC and silently unscopes every later query.

## Where the depth lives

| Topic | Read |
|---|---|
| Invariants 1–14 with enforcing artifacts; case lifecycle; weak points | `../../.claude/skills/stoop-architecture-contract/SKILL.md` |
| Frozen-artifact change procedures; reviewer gates; merge protocol | `../../.claude/skills/stoop-change-control/SKILL.md` |
| Symptom → triage for every known backend failure mode | `../../.claude/skills/stoop-debugging-playbook/SKILL.md` |
| Core-loop issue implementation guides (#44/#45/#108/#50/#111 remaining) | `../../.claude/skills/stoop-core-loop-campaign/SKILL.md` |
| Test/eval evidence bar; golden tests; eval-gate contract | `../../.claude/skills/stoop-validation-and-qa/SKILL.md` |
| Env vars, boot gates, constants (budgets, cooldowns, windows) | `../../.claude/skills/stoop-config-and-flags/SKILL.md` |
| Live-DB discipline, operator flips, migration dry-runs | `../../.claude/skills/stoop-run-and-operate/SKILL.md` |
