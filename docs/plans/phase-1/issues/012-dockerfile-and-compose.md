---
title: "feat(infra): Dockerfile and docker-compose for local dev"
labels: ["phase-1", "type-deployment", "infra", "size-s"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Multi-stage `Dockerfile` for the backend (used by Fly.io) and a `docker-compose.yml` for local dev environments without depending on Supabase being reachable.

## Why this matters

Fly.io needs a Dockerfile to deploy. Local docker-compose with a local Postgres lets you develop offline and test migrations without polluting your dev Supabase.

## Acceptance criteria

- [ ] `backend/Dockerfile` is multi-stage: builder stage installs deps, final stage is minimal
- [ ] Final image is non-root user
- [ ] Image runs `uvicorn app.main:app --host 0.0.0.0 --port 8080` by default
- [ ] Image works locally: `docker build -t stoop-backend . && docker run -p 8080:8080 stoop-backend` starts the server
- [ ] `.dockerignore` excludes `.venv/`, `__pycache__/`, `.env`, `.git/`, tests, docs
- [ ] `docker-compose.yml` at repo root brings up Postgres 15 with a sensible password
- [ ] `docker-compose up -d` exposes Postgres on port 5432 to localhost
- [ ] Volume for Postgres data persists across restarts
- [ ] Image size under 300MB final (verify with `docker images`)
- [ ] `backend/README.md` updated with Docker run instructions

## Out of scope

- Don't deploy yet — issue #13
- Don't add Inngest dev server to docker-compose — Phase 4
- Don't add hot-reloading in Docker (use uv locally for dev — Docker is for parity testing and production)

## Effort & dependencies

- **Effort:** S (3-4 hours, mostly Docker image size optimization)
- **Blocks:** #13
- **Blocked by:** #5

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Why multi-stage?** Builder has dev tools (compilers, build deps); final stage doesn't. Smaller image, smaller attack surface. The split is: builder installs deps into a venv; final copies the venv but not the build toolchain.

2. **What base image?** `python:3.12-slim` is the go-to. Smaller than `python:3.12`, fewer CVEs.

3. **Should local dev use Docker or run uv directly?** Local dev: run `uvicorn` directly via uv (fastest iteration). Use Docker only for: parity testing before deploy, running Postgres locally without Supabase, CI builds.

</details>

<details>
<summary><b>Hints</b></summary>

- Multi-stage Dockerfile pattern: first stage `FROM python:3.12-slim AS builder`, second stage `FROM python:3.12-slim`. Copy from builder.
- Install uv inside the builder stage: `RUN pip install uv` (it's the simplest install path inside Docker)
- Copy `pyproject.toml` and `uv.lock` first, then run `uv sync --frozen` — Docker caches the layer until the lockfile changes
- Copy app source last so code changes don't invalidate the dep cache
- Create a non-root user: `RUN useradd -m -u 1000 app && chown -R app:app /app && USER app`
- For docker-compose, Postgres image `postgres:15`, set `POSTGRES_DB=stoop`, `POSTGRES_USER=stoop`, `POSTGRES_PASSWORD=stoop` (this is local-only)
- Use a named volume for Postgres data so it survives restarts

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't `COPY . .` early in the Dockerfile — invalidates layer cache on every code change. Copy deps first, sync, then copy code.
- Don't run as root in production. Add a non-root user.
- Don't bake secrets into the image (no `ENV CLERK_SECRET_KEY=...`). Secrets come from Fly secrets at runtime.
- `--frozen` on `uv sync` is important — without it, uv may re-resolve deps and break reproducibility
- `python:3.12-slim` vs `python:3.12-alpine` — slim (Debian-based) is more compatible; alpine breaks some Python wheels with musl libc. Stick with slim.
- Port 8080 vs 8000: Fly's default expectation is 8080. Override in code OR in `fly.toml` — but pick one and stick.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my `backend/Dockerfile` and `docker-compose.yml`:
> 1. Is multi-stage build optimal — could I shrink further?
> 2. Is the layer cache ordering correct?
> 3. Is non-root user setup right (UID, file permissions)?
> 4. Any security issues with the docker-compose setup (default password, exposed ports)?
> 5. Are there Python-specific Docker patterns I'm missing (PYTHONDONTWRITEBYTECODE, etc.)?

</details>
