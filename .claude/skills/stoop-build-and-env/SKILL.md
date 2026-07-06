---
name: stoop-build-and-env
description: Recreate the Stoop dev environment from scratch and avoid its environment traps. Load this when you are on a fresh clone or new machine; when you need to install prerequisites (uv, Python 3.12, Docker Desktop, Bun); when running uv sync, docker compose up, alembic upgrade head, uvicorn, bun install, or pre-commit install for the first time; when .env setup or missing credentials block you; when pytest suddenly shows dozens of connection errors ("204 errors" style) or hits the placeholder test:test database; when you are tempted to `source .env`, `cat .env`, or run `uv run pytest` and wonder what the eval marker does (read this first); when you can't find docker-compose.yml in apps/api (it's at repo root) or a sqlalchemy.url in alembic.ini (URL comes from app config/env); when Docker Desktop wedges; or when two agents need to work on the repo in parallel (one agent per working tree; git worktrees). Setup + environment only — for operating against the live Supabase DB see stoop-run-and-operate.
---

# Stoop — build and environment from scratch

Repo root: the `LandlordAI` monorepo, cloned from `https://github.com/LaithAlz/stoop-backend.git`
(`apps/api` = Python/FastAPI backend, `apps/web` = Bun/TanStack frontend, `docs/` = source of
truth). All commands below are repo-relative and copy-pasteable. This skill gets a zero-context
agent from `git clone` to a running backend and web app, and encodes the environment traps that
burned previous sessions. Volatile facts are date-stamped.

## When NOT to use this skill

| You are trying to... | Load instead |
|---|---|
| Run, migrate, or operate against the **live Supabase** DB; operator flips (`APP_DATABASE_URL`, `app_role` LOGIN); the live dry-run protocol | `stoop-run-and-operate` |
| Understand which env vars/settings exist, defaults, boot gates; add a config axis | `stoop-config-and-flags` |
| Decide what test/eval evidence counts before shipping; run the eval gate | `stoop-validation-and-qa` |
| Triage a failure you don't recognize | `stoop-debugging-playbook` |
| Read the full incident history behind these rules | `stoop-failure-archaeology` |
| Ship a change (gates, reviewers, PR flow) | `stoop-change-control` |

## 1. Prerequisites

| Tool | Version | Pin source | Notes |
|---|---|---|---|
| **uv** | any recent (unpinned) | `apps/api/uv.lock` committed; CI uses `astral-sh/setup-uv@v8.2.0` + `uv sync --frozen` | The only Python tool you install by hand; it manages Python + deps. |
| **Python** | **3.12 hard-pinned** | `apps/api/pyproject.toml` `requires-python = ">=3.12,<3.13"` + `apps/api/.python-version` = `3.12` | Do not use 3.13. `uv sync` installs the right interpreter automatically. |
| **Docker Desktop** | any recent with Compose v2 (`docker compose`, not `docker-compose`) | — | Runs local Postgres for migrations + integration tests. |
| **Bun** | **UNPINNED — flag** | none: no `engines`, no `packageManager` in `apps/web/package.json`, no `.nvmrc` anywhere (as of 2026-07-06) | `bun.lock` pins packages but nothing pins Bun/Node itself. If web builds behave differently across machines, suspect Bun version drift first. |

There is **no root monorepo tooling**: no Makefile, no root `package.json`, no turbo/nx;
`packages/` contains only a `.gitkeep` (as of 2026-07-06). Each app is set up independently.

## 2. Backend setup runbook (`apps/api`)

Run these in order. Each step's expected outcome is noted.

```bash
# 1. Clone and enter the API app
git clone https://github.com/LaithAlz/stoop-backend.git LandlordAI && cd LandlordAI/apps/api

# 2. Install deps (also installs Python 3.12 if missing)
uv sync

# 3. Create the env file from the template
cp .env.example .env
```

**STOP GATE — credentials.** `.env` needs real Supabase / Twilio / Anthropic values that only a
human can create (per `apps/api/CLAUDE.md`: "If a task needs credentials that don't exist, stop
and say so"). Agents do not invent, guess, or fetch credentials — stop and ask the human to fill
`.env`. All of `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER`,
`SUPABASE_SERVICE_ROLE_KEY`, `TWILIO_AUTH_TOKEN`, `ANTHROPIC_API_KEY` are **required at boot**
(declared with no defaults in `app/config.py`; a missing one raises `pydantic.ValidationError`
at import). `APP_DATABASE_URL`, `PUBLIC_BASE_URL`, `LANGSMITH_*`, `SENTRY_DSN` are optional
locally — but the first two become boot-gated REQUIRED when `ENVIRONMENT=production`
(see `stoop-config-and-flags`). For pure-local work set
`DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop` (the Docker DB below).

```bash
# 4. Start local Postgres — NOTE: docker-compose.yml lives at REPO ROOT, not apps/api
cd ../.. && docker compose up -d           # i.e. run from LandlordAI/
# postgres:15, db/user/password = stoop/stoop/stoop, port 5432, volume stoop_pgdata

# 5. Migrate (URL comes from app config / DATABASE_URL env — NOT alembic.ini)
cd apps/api && uv run alembic upgrade head
# Expected: applies 0001 → 0008 (head as of 2026-07-06)

# 6. Run the API
uv run uvicorn app.main:app --reload
# Serves on http://127.0.0.1:8000

# 7. Verify (separate shell)
curl -s http://127.0.0.1:8000/healthz   # -> {"status":"ok"}  (liveness, no deps)
curl -s http://127.0.0.1:8000/readyz    # -> 200 when DB reachable, 503 otherwise

# 8. Lint + types must be green before any commit
uv run ruff check . && uv run mypy app
```

`.env` auto-loading is **cwd-relative**: `app/config.py` uses pydantic-settings with
`env_file=".env"`, so uvicorn/alembic/pytest pick up `.env` only when run **from `apps/api`**.
Real environment variables override `.env` values (pydantic-settings precedence) — that is why
the step-5 export pattern in §6 works.

`docker compose down` stops Postgres (data persists in the volume); `docker compose down -v`
wipes it. The stoop/stoop credentials are intentionally trivial and local-only — never reuse
them anywhere real.

## 3. Web setup (`apps/web`)

```bash
cd apps/web
bun install
bun run dev      # local dev server (vite)
bun run build    # production build (Cloudflare Workers target)
bun run lint     # eslint .
```

Flags to know (as of 2026-07-06):

- **No `test` or `typecheck` script exists** in `apps/web/package.json` (scripts are `dev`,
  `build`, `build:dev`, `preview`, `lint`, `format`). Type errors surface only via
  `bun run build` or the editor. Do not claim "web tests pass" — there are none to run.
- **No web CI exists.** `.github/workflows/ci.yml` has only the backend job. Green CI on a PR
  says nothing about the web app; run `bun run build && bun run lint` manually before shipping
  web changes.

## 4. Tooling: pre-commit hooks

One-time setup (from repo root — `.pre-commit-config.yaml` lives there):

```bash
uv tool install pre-commit && pre-commit install
```

What the hooks run vs. what CI runs — the difference matters:

| Check | pre-commit (local, MUTATING) | CI (`.github/workflows/ci.yml`, non-mutating) |
|---|---|---|
| Lint | `uv run --directory apps/api ruff check --fix .` (auto-fixes) | `uv run ruff check .` (fails, fixes nothing) |
| Format | `uv run --directory apps/api ruff format .` (rewrites files) | `uv run ruff format --check .` (fails only) |
| Types | `uv run --directory apps/api mypy app` (strict) | `uv run mypy app` (same) |
| Secrets | `gitleaks` (rev v8.30.1 as of 2026-07-06) | **not in CI** — the hook is the ONLY automated secret gate; never commit with `--no-verify` |

Consequences:

- If a hook mutates files, the commit fails; re-stage the **specific** changed files and
  re-commit. Stage explicitly — never `git add .` (session-verified 2026-07-05; no repo
  artifact).
- A commit made without hooks installed can fail CI on format. If CI fails on
  `ruff format --check`, run `uv run ruff format .` in `apps/api` and re-commit.
- Hooks run via `uv run`, so ruff/mypy versions match `uv.lock` exactly — no drift between
  local, hooks, and CI.

## 5. THE .ENV DISCIPLINE (never-break)

The incident that made this a hard rule: `source .env` under zsh errored on a line and
**echoed the live Twilio auth token into the terminal/conversation log**. The token had to be
treated as compromised and rotated in the Twilio console
(session-verified 2026-07-05; no repo artifact).

Rules, absolute:

1. **Never `source .env`.** Ever. Not "carefully", not with `set +x`.
2. **Never print env values** — no `cat .env`, no `echo $TWILIO_AUTH_TOKEN`, no `env | grep`,
   no logging the settings object (`app/config.py`'s docstring says the same). Discovery/
   exploration passes never read `.env`. This is project never-break rule 5 (no secrets in
   logs) applied to the shell.
3. **Never commit `.env`.** It is gitignored (repo-root `.gitignore`); the gitleaks pre-commit
   hook backstops you. Never `git add .env`.
4. Usually you need **nothing at all**: commands run from `apps/api` get `.env` auto-loaded by
   pydantic-settings (§2). Reach for the pattern below only when a command reads raw process
   env vars.
5. When a command genuinely needs `.env` values as env vars, use this exact pattern: Python
   parses the file, `shlex.quote`s each value, and prints `export` lines consumed directly by
   `eval "$(...)"` — values never touch the terminal (pattern session-verified 2026-07-05; no
   repo artifact; snippet mechanically re-tested against a dummy file 2026-07-06):

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
)" && cd apps/api && <your ONE command here>
```

Keep the `eval` and the command **on one shell line**: env vars do not survive across separate
tool calls (each Bash call is a fresh shell), and the working directory can reset too — `cd`
explicitly every time.

For the non-secret local Docker DB URL only, a plain inline export is fine (see §6) — those
credentials are intentionally public-trivial.

## 6. Test invocation discipline

**Safe default (unit + everything non-paid):**

```bash
cd apps/api && uv run pytest -m "not eval" -q; echo EXIT=$?
```

- **Bare `uv run pytest` collects the PAID eval marker.** `pyproject.toml` defines markers
  `unit` / `integration` / `eval`; since PR #177 (2026-07-06) `addopts = "-m 'not eval'"` excludes `eval` by default —
  `tests/test_evals.py` carries `@pytest.mark.eval` tests that hit the real Anthropic API (the
  file itself says: "THE PAID GATE -- @pytest.mark.eval -- NEVER RUN THESE FROM AN AGENT").
  Paid eval runs need the founder's go-ahead and are orchestrator-only (session-verified
  2026-07-05; no repo artifact). Always pass `-m "not eval"`.

**Integration tests** need Docker Postgres up (§2 step 4) AND `DATABASE_URL` exported
**in the same shell line** as pytest:

```bash
cd apps/api && export DATABASE_URL=postgresql+asyncpg://stoop:stoop@localhost:5432/stoop && uv run pytest -m "not eval" -q; echo EXIT=$?
```

**The placeholder-DB false-alarm signature.** `tests/conftest.py` runs
`os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")`
(plus placeholder Supabase/Twilio/Anthropic values) **before any app import** — and since real
env beats the `.env` file, your `.env` does NOT save you here. Forget the export and
integration tests spray dozens of connection ERRORs that look like mass breakage ("204
errors", "37 errors" scares in past sessions — session-verified 2026-07-05; no repo artifact).
Diagnostic: if the error text mentions `test:test@localhost:5432/test`, the diagnosis is
*missing export in this shell invocation*, not broken code. CI does not have this problem
because `ci.yml` sets `DATABASE_URL` at the job level.

**Never pipe pytest to `tail`/`head`.** A pipe returns the *last* command's status, so
`uv run pytest ... | tail` reports tail's success and masks red tests (session-verified
2026-07-05; no repo artifact). Run un-piped with `-q` and always end the pytest segment with
`; echo EXIT=$?`, as in the one-liners above.

**Stray-database trap:** a leftover sibling database created by a review agent
(`stoop_review`) once made the pg_shdepend role-drop test fail (session-verified 2026-07-05;
no repo artifact). If role/grant tests fail mysteriously, list databases and drop strays.

Sanity check without running anything: `uv run pytest --collect-only -q`
(the count drifts with every PR and differs across branches — the test
inventory home is `stoop-validation-and-qa`). Deeper failure triage →
`stoop-debugging-playbook`.

## 7. Parallel work: one agent per working tree

The incident: a frontend agent ran `git checkout` / `git stash` in the main working tree while
another agent had uncommitted eval work there — the work was destroyed mid-flight and had to be
recovered via reflog and manual rework (session-verified 2026-07-05; no repo artifact).
Standing rule since, founder-elevated to never-break: **one active agent per working tree.**
Any parallel branch work gets its own worktree, and nobody runs branch-switching or stash
commands in a tree they don't exclusively own:

```bash
git worktree add ../LandlordAI-<topic> -b <branch>    # new branch in its own tree
git worktree remove ../LandlordAI-<topic>             # when done (tree must be clean)
```

## 8. Environment facts (the ones that surprise people)

- **`docker-compose.yml` is at REPO ROOT**, not `apps/api`. `docker compose up -d` from
  `apps/api` finds nothing; run it from the root (or `docker compose -f ../../docker-compose.yml up -d`).
- **No root monorepo tooling** — no Makefile, no root package.json, no shared task runner.
  `apps/api` (uv) and `apps/web` (bun) are independent.
- **Alembic's DB URL does not come from `alembic.ini`.** `sqlalchemy.url` is intentionally
  absent there; `migrations/env.py::get_url()` resolves it from `app.config`
  `settings.database_url`, falls back to the raw `DATABASE_URL` env var, and normalizes
  `postgresql://` → `postgresql+asyncpg://`. Migrations therefore obey the same `.env` /
  same-shell-line export discipline as everything else.
- **Local Docker Postgres runs as a bootstrap SUPERUSER and is therefore BLIND to Supabase
  privilege behavior.** A role/grant/RLS migration that is green locally can fail on live
  Supabase, where the `postgres` role is *not* superuser — exactly what happened to migration
  `0004_auth_users_lifecycle_trigger.py`, whose original design failed live with a
  `must be able to SET ROLE` error and was redesigned around a SECURITY DEFINER function
  (session-verified 2026-07-05; the shipped redesign is the repo artifact). Standing rule: any
  migration touching roles, grants, or RLS gets a **live Supabase dry-run before merge** — the
  protocol lives in `stoop-run-and-operate`.
- **Docker Desktop wedges occasionally** (three times as of 2026-07-06). Recognition
  signatures: `docker compose up -d` hangs / the CLI can't reach the daemon — OR
  integration tests failing en masse with asyncpg connect `TimeoutError`s against the
  CORRECT `stoop:stoop@localhost:5432/stoop` URL (the third wedge presented as 44 such
  "failures"; not the `test:test` placeholder shape). Recovery every time:
  `pkill -f Docker; open -a Docker`, wait for the daemon, then `docker compose up -d`
  (session-verified; no repo artifact).
- CI runs migrations and tests against the same `postgres:15` / stoop/stoop/stoop shape as
  local Docker, so "works in CI, fails on live Supabase" has the same superuser-blindness
  cause as above.

## Provenance and maintenance

Volatile claims and how to re-verify each in one line (all from repo root unless noted):

| Claim (as of 2026-07-06) | Re-verify with |
|---|---|
| Python pin `>=3.12,<3.13` | `grep requires-python apps/api/pyproject.toml` |
| `.python-version` = 3.12 | `cat apps/api/.python-version` |
| No Bun/Node version pin | `grep -E '"engines"\|"packageManager"' apps/web/package.json; ls apps/web/.nvmrc 2>&1` |
| Compose at root: postgres:15, stoop/stoop/stoop:5432 | `grep -n -A6 'image: postgres' docker-compose.yml` |
| Migration head = 0008 | `cd apps/api && uv run alembic history \| head -2` |
| Collected-test count (drifts — inventory home is `stoop-validation-and-qa`); eval marker exists | `cd apps/api && uv run pytest --collect-only -q \| tail -2` |
| addopts guard present (bare pytest excludes eval) | `grep -n addopts apps/api/pyproject.toml` (expect `-m 'not eval'`) |
| Paid-gate warning in test file | `grep -n 'PAID GATE' apps/api/tests/test_evals.py` |
| Placeholder DB URL `test:test@localhost:5432/test` | `grep -n 'test:test@' apps/api/tests/conftest.py` |
| Pre-commit hooks: ruff --fix / ruff format / mypy app / gitleaks | `grep -n 'id:\|entry:\|rev:' .pre-commit-config.yaml` |
| CI is non-mutating (`--check`), backend-only, no gitleaks | `grep -n 'ruff\|jobs:\|gitleaks' .github/workflows/ci.yml; ls .github/workflows/` |
| Web scripts lack test/typecheck | `python3 -c "import json;print(json.load(open('apps/web/package.json'))['scripts'])"` |
| Required-at-boot settings (no defaults) | `grep -n 'sensitive — no default' apps/api/app/config.py` |
| `/healthz` + `/readyz` endpoints | `grep -n 'healthz\|readyz' apps/api/app/routers/health.py` |
| Alembic URL source (not alembic.ini) | `grep -n 'sqlalchemy.url' apps/api/alembic.ini; grep -n 'DATABASE_URL' apps/api/migrations/env.py \| head -3` |
| `.env` gitignored | `grep -n '^\.env$' .gitignore` |
| Clone URL | `git remote -v` |
