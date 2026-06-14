# stoop-api

FastAPI backend for Stoop — sorts tenant texts, drafts replies in the
landlord's voice, and rings the landlord only for true emergencies.

```bash
uv sync                                # install deps
uv run uvicorn app.main:app --reload   # run dev server (app skeleton lands in #5)
uv run pytest                          # tests
```

## Local Postgres

Integration tests and migrations run against a local Postgres so you never
touch (or pollute) the Supabase dev database. The compose file lives at the
repo root:

```bash
docker compose up -d        # Postgres on localhost:5432 (db/user/pass: stoop)
docker compose down         # stop; data persists in the named volume
docker compose down -v      # stop and wipe the data volume
```

## Docker (parity / production image)

The `Dockerfile` is the image Fly.io deploys (#13). Local dev should use `uv`
directly (above) — Docker is for parity testing and production. Build context
is this directory:

```bash
docker build -t stoop-api .
docker run -p 8080:8080 --env-file .env stoop-api   # serves on :8080
```

The image is multi-stage (production deps only), runs as a non-root `app`
user, and binds `0.0.0.0:8080`. It needs the same env vars as local dev;
without them the app exits at startup by design.
