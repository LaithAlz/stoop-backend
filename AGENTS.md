# Stoop — agent operating guide

You are working in the Stoop monorepo (AI tenant-maintenance handling for
landlords). This file is the entry point: hard rules, safe commands, the ship
flow, and a router to the deep reference docs. Nested `AGENTS.md` files in
`apps/api/` and `apps/web/` add app-specific rules. Also read `CLAUDE.md`
(root) and `apps/api/CLAUDE.md` — they are binding project instructions, not
tool-specific files. Volatile facts below are stamped "as of 2026-07-12".

## What this repo is (10 lines)

- Product: tenants text one phone number; Stoop classifies every message
  (EMERGENCY / URGENT / ROUTINE), drafts replies in the landlord's voice, and
  rings the landlord's phone only for a true emergency.
- `apps/api` — Python 3.12 / FastAPI / async SQLAlchemy / LangGraph backend,
  managed with `uv`. Target host Fly.io. `apps/web` — TanStack Start +
  shadcn/ui dashboard and marketing site, Bun, Cloudflare Workers.
- `docs/` is the **source of truth** — code follows docs, never vice versa.
- GitHub: `LaithAlz/stoop-backend`. CI (`.github/workflows/ci.yml`) covers the
  backend ONLY: `uv sync --frozen` → `ruff check` → `ruff format --check` →
  `mypy app` → `alembic upgrade head` → `pytest -m "not eval"`.
- Current state (as of 2026-07-12): agent graph wired with shadow-mode
  `interrupt()` before any send (PRs #185/#187); degraded mode built (#188);
  migrations 0001–0009; **no outbound-send code exists yet** (#108 in
  flight); the web dashboard renders the Clarity design over mock data.
- System map, invariants, and weak points:
  `.claude/skills/stoop-architecture-contract/SKILL.md`.

## Hard rules — never break (each with its enforcing artifact)

1. **The emergency line is never paywalled, throttled, or gated.** No feature
   flag or billing read may sit on the emergency path (`app/agent/` reads no
   flags at all — `app/agent/emergency.py` docstring).
2. **`messages`, `audit_log`, `message_status_events` are append-only.** The
   DB itself refuses: `REVOKE UPDATE, DELETE` in migration
   `apps/api/migrations/versions/0005_app_role_and_rls.py`. Never fight it.
3. **Nothing sends to a tenant or vendor without landlord approval**, except
   emergency safety instructions. Exactly two send paths are sanctioned
   forever: the draft-approval flow and the emergency safety path. Never add
   a third Twilio egress call site.
4. **The severity rubric is embedded verbatim and frozen.**
   `apps/api/tests/test_rubric.py` enforces byte-equality with
   `docs/02-product/severity-rubric-v1.md` plus a pinned sha256. Any prompt
   or rubric change = new version file + full (paid, founder-gated) eval run.
5. **Never log JWTs, tenant phone numbers, or message bodies** — app logs,
   Sentry, error messages, terminal output. `app/observability.py` pins
   `send_default_pii=False` AND `include_local_variables=False`; the webhook
   logs a keyed HMAC digest, never a number. Row uuids are enough.
6. **Schema names come from `docs/03-engineering/schema-v1.md`.** New column
   = edit that doc first, then the migration. Never invent a name.
7. **Analytics discipline:** PostHog identifies by landlord uuid only; no
   PII in event properties; session replay off. **Feature flags never gate
   safety behavior** — flag-service failure must look exactly like flags-off.
8. **Customer-facing copy:** plain English (never "triage"), no legal/LTB
   mentions on marketing pages, never "founding/cohort/spot counts" — say
   "early access". Exact prices: free Emergency Line / $10 Full Plan /
   $5 early-access (grandfathered) / PMs $1.50/door. Rules doc:
   `docs/02-product/plain-language-rules.md`.

Founder-elevated (same force as the eight above):

9. **Paid eval runs are founder-gated.** A real run costs ~74–85¢ and 15–25
   min (as of 2026-07-06). Never fire one autonomously. Machinery testing is
   free with `EVAL_DRY_RUN=1`.
10. **Live Supabase dry-run before merging any migration touching roles,
    GRANTs, or RLS.** Local Docker Postgres runs as superuser and is blind to
    privilege bugs (migration 0004 passed locally, failed live).
11. **The author never approves its own change.** Independent review passes
    (spec, safety, copy — see ship flow) are mandatory; safety review uses
    the strongest model available, never the one that wrote the code.
12. **One active agent per working tree.** Parallel work gets its own
    `git worktree add ../LandlordAI-<topic> -b <branch>`. Never run
    `git checkout`/`git stash` in a tree another agent is using.

## Safe commands (exact forms — deviations have burned sessions)

**Tests** — env vars do NOT survive between shell invocations and cwd can
reset, so every run is ONE self-contained command with the exit code printed:

```bash
cd apps/api && export DATABASE_URL='postgresql+asyncpg://stoop:stoop@localhost:5432/stoop' && uv run pytest -m "not eval" -q; echo EXIT=$?
```

- Local Postgres first: `docker compose up -d` — the compose file is at the
  **repo root**, not `apps/api` (db/user/password all `stoop`, port 5432).
- Bare `uv run pytest` is safe: `apps/api/pyproject.toml` carries
  `addopts = "-m 'not eval'"`. But **NEVER run `pytest -m eval` and NEVER run
  `python -m evals.runner` without `EVAL_DRY_RUN=1`** — both hit the real
  Anthropic API and cost money (rule 9). The paid gate is a human decision.
- Never pipe pytest to `tail`/`head` — the pipe returns tail's exit status
  and masks failures. Always end with `; echo EXIT=$?`.
- If dozens of tests ERROR against `test:test@localhost:5432/test`, you
  forgot the `DATABASE_URL` export in THIS command — not real breakage.

**Secrets** — never `source .env`, never `cat`/`echo` env values, never read
`.env` during exploration (a sourced `.env` once echoed a live Twilio token
and forced a rotation). Env sanity without printing values:
`.claude/skills/stoop-diagnostics-and-tooling/scripts/env-check.sh`.

**Docker wedge recovery** — `docker compose up -d` hangs, or integration
tests spray asyncpg `TimeoutError`s against the CORRECT `stoop:stoop` URL:
`pkill -f Docker; open -a Docker`, wait for the daemon, then from repo root
`docker compose up -d`.

**Git** — read-only except your own branch. Stage files explicitly, never
`git add .`. Docs-only commits may push to `main`; app code never does.

## The ship flow (app code — no exceptions)

1. Branch off fresh `main`: `<type>/<short-desc>` (feat/fix/chore/ci/docs,
   lowercase, hyphenated, ≤40 chars).
2. Read the GitHub issue (`gh issue view <N>`), root + app instructions, and
   the docs the issue cites. Scope to exactly what was asked.
3. Build with tests; keep `ruff check`, `ruff format`, `mypy app` green
   (web: `bun run lint && bun run build` — there is no web CI).
4. Review passes BEFORE the PR, by a reviewer that is not the author:
   spec conformance (schema-v1, api-contracts, the rules above, the issue's
   acceptance criteria); **safety review is mandatory** for anything touching
   auth/JWT, RLS, webhooks, `app/agent/`, `app/deps.py`, emergency or
   approval flows; copy review for any customer-visible string.
5. Commit: conventional-ish subject (`feat(api): …`), imperative, ≤72 chars,
   no filler, no AI attribution, explicit staging.
6. `git push -u origin <branch>` then `gh pr create --repo
   LaithAlz/stoop-backend` — body: What / Why (`Closes #N`) / Test plan /
   Notes, with review verdicts pasted in (the PR is the audit record).
7. Watch CI: `gh pr checks <N> --repo LaithAlz/stoop-backend --watch`.
8. Verify green **on the tip commit** — a PR was once merged on a stale
   commit's green: `TIP=$(gh pr view <N> --repo LaithAlz/stoop-backend
   --json headRefOid -q .headRefOid)` then check
   `repos/LaithAlz/stoop-backend/commits/$TIP/check-runs` all read
   `completed success`.
9. Red check → read the log, fix, push, re-watch. Never merge red; there is
   no override path. Blocking review findings → fix and re-review.
10. Merge (human gate / standing founder directive only):
    `gh pr merge <N> --repo LaithAlz/stoop-backend --squash --delete-branch`.

## Frozen artifacts — never edit in place

| Artifact | Change procedure |
|---|---|
| `apps/api/app/agent/rubric.py` + the rubric block in `docs/02-product/severity-rubric-v1.md` | New version file pair + new pinned sha + full eval run + founder gate |
| `apps/api/app/agent/prompts/v1.py` and `v2.py` | Add `v3` (the next free version as of 2026-07-12) + full eval run + founder gate |
| Eval scenarios `apps/api/evals/scenarios/*.yaml` + scoring rules in `docs/02-product/eval-scenarios-v1.md` | Adding scenarios: encouraged. Weakening/removing: doc amendment + founder gate + full eval run |
| `apps/api/app/agent/prefilter.py` pattern set | Additive-only, zero HARD→silent flips, regression test class per change, eval `prefilter_must_fire` stays green |

## Router — read the deep doc BEFORE you act

The files under `.claude/skills/<name>/SKILL.md` are **plain markdown
reference docs** (~4,700 lines of institutional knowledge). The `.claude/`
path and the YAML header at the top of each are conventions of a different
agent tool — ignore them; open the files with your normal file tools and read
them like any other doc. They are dense, current, and each ends with
re-verification commands for its volatile claims.

| Before / when you are… | Read |
|---|---|
| Creating a branch/PR, merging, touching anything frozen, asking "can I just push this" / "do I need a safety review" / "can I run the paid evals" | `.claude/skills/stoop-change-control/SKILL.md` |
| On a fresh clone; installing uv/Docker/Bun; first `uv sync` / `docker compose up` / `alembic upgrade head`; `.env` setup; pre-commit hooks | `.claude/skills/stoop-build-and-env/SKILL.md` |
| Debugging ANY test failure in `apps/api` (rule out environment traps first); hitting any error signature in the table below | `.claude/skills/stoop-debugging-playbook/SKILL.md` |
| Touching the webhook, prefilter, graph/nodes, drafts queue, RLS, migrations, JWKS auth, LLM budget, checkpointer; asking "how does Stoop work" / "what invariants must I not break" | `.claude/skills/stoop-architecture-contract/SKILL.md` |
| Adding/changing/reading an env var or Settings field; boot-gate refusals in production; eval knobs; hardcoded constants; wrangler config | `.claude/skills/stoop-config-and-flags/SKILL.md` |
| Implementing or planning issues #34/#43/#44/#45/#50/#108/#109/#111 — graph wiring, approve/reject/send, escalation chain, degraded mode, e2e test, cost metering | `.claude/skills/stoop-core-loop-campaign/SKILL.md` |
| Checking the eval gate verdict, reading `last-run.json`, testing a phrase against Tier-0, querying `audit_log` decisions, probing RLS/grants | `.claude/skills/stoop-diagnostics-and-tooling/SKILL.md` |
| Editing anything under `docs/`, writing customer-facing words, making public claims, prices, "can we say X" | `.claude/skills/stoop-docs-and-writing/SKILL.md` |
| Needing domain background: severity rubric doctrine, refusal flags, LTB, CASL/A2P, SMS plain-language rules, trust ladder, Supabase platform behavior | `.claude/skills/stoop-domain-reference/SKILL.md` |
| About to re-open a past design decision, remove a "weird" knob, re-add `temperature`, switch RLS to FORCE, or seeing a historical-smelling symptom | `.claude/skills/stoop-failure-archaeology/SKILL.md` |
| About to trust an idempotency/dedupe/exactly-once claim, review a sweeper (TOCTOU), or merge a safety claim on the author's word | `.claude/skills/stoop-proof-and-analysis-toolkit/SKILL.md` |
| Judging evidence quality, predicting/interpreting eval numbers, growing the scenario corpus, "zero missed emergencies" claims | `.claude/skills/stoop-research-and-frontier/SKILL.md` |
| Starting the server, running/writing migrations, anything against LIVE Supabase, operator flips, webhook operations, deploy state | `.claude/skills/stoop-run-and-operate/SKILL.md` |
| About to claim "tests pass / safe to merge"; adding eval scenarios or regression tests; touching `apps/api/tests/` or `apps/api/evals/`; QA for `apps/web` | `.claude/skills/stoop-validation-and-qa/SKILL.md` |

Error signatures → doc (all rows: `.claude/skills/<name>/SKILL.md`):

| You see… | Read |
|---|---|
| `DuplicatePreparedStatementError` against Supabase port 6543 | `stoop-debugging-playbook` (never remove the three pooler knobs — settled battle) |
| Flaky/intermittent 401s in auth tests | `stoop-debugging-playbook` |
| Dozens of ERRORs mentioning `test:test@localhost:5432/test` | `stoop-debugging-playbook` (missing same-shell `DATABASE_URL` export) |
| Mass asyncpg `TimeoutError` on the correct `stoop:stoop` URL; compose hangs | `stoop-debugging-playbook` (Docker wedge — recovery recipe above) |
| `psycopg_pool.PoolClosed` from the LangGraph checkpointer | `stoop-debugging-playbook` (ordering contract, not an outage) |
| RLS handler suddenly returns zero rows / GUC reads empty mid-handler | `stoop-debugging-playbook` (mid-handler commit killed `SET LOCAL`) |
| `/webhooks/twilio/sms` returns 5xx and you want to "fix" it to 200 | `stoop-debugging-playbook` — 5xx is BY DESIGN; Twilio retry is the recovery |
| `DeadlockDetectedError` in `test_downgrade_removes_table` | `stoop-debugging-playbook` (known open flake #174 — re-run, don't fix) |
| A `*_double_timeout_shares_one_end_to_end_deadline` test fails | `stoop-debugging-playbook` (load-sensitive flake — re-run standalone first) |
| Eval scenario `ERROR (inconclusive)` / 429 / 529 | `stoop-debugging-playbook` (infra failure ≠ rubric miss) |
| LLM judge prose says PASS but booleans say FAIL | `stoop-debugging-playbook` + `stoop-diagnostics-and-tooling` (harness bug, never the rubric) |
| `must be able to SET ROLE`; migration green locally, fails on live Supabase | `stoop-failure-archaeology` (A2) + `stoop-run-and-operate` (live dry-run protocol) |
| Pydantic `extra_forbidden` on wrapper-keyed LLM tool output | `stoop-failure-archaeology` (A8/A22 — one-validator composition trap) |

## Diagnostic scripts (tested, read-only, secret-safe)

| Script (run from repo root) | Answers |
|---|---|
| `.claude/skills/stoop-diagnostics-and-tooling/scripts/env-check.sh` | Is my toolchain/env sane? uv present, Docker daemon up, compose Postgres healthy, required `.env` var NAMES present — never prints a value; exit code = failure count |
| `python3 .claude/skills/stoop-diagnostics-and-tooling/scripts/eval-summary.py` | Did the eval gate pass? Compact per-scenario table + `release_blocked` verdict + judge-inversion warnings from `apps/api/evals/results/last-run.json`; exit 0 = clear |
| `docker compose exec -T postgres psql -U stoop -d stoop < .claude/skills/stoop-diagnostics-and-tooling/scripts/db-probes.sql` | What does the DB actually enforce? alembic head, RLS status/policies, role flags, append-only grants, latest audit actions (local Docker or explicitly-authorized live reads only) |
