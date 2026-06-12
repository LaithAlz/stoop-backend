---
title: "feat(infra): deploy to Fly.io (stoop-dev in yyz region)"
labels: ["phase-1", "type-deployment", "infra", "size-m", "gate"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Deploy the backend to Fly.io in the Toronto (`yyz`) region. Verify `GET /v1/me` works against the production deployment with a real Supabase access token.

**This closes the Phase 1 gate** along with #11 and #14.

## Why this matters

A deployed, authenticated endpoint hit from a real client is the milestone you can point to. Everything before this is "code on my laptop."

## Acceptance criteria

- [ ] Fly app `stoop-dev` exists in region `yyz`
- [ ] `fly.toml` committed with multi-process config (web only for Phase 1; worker added in Phase 4)
- [ ] All secrets set via `fly secrets set ...` (database URL, Supabase service-role key, Sentry DSN)
- [ ] No secrets committed in `fly.toml` — all in `fly secrets`
- [ ] Health checks configured: `/healthz` for liveness, `/readyz` for readiness
- [ ] Deploy succeeds: `fly deploy` exits 0
- [ ] `curl https://stoop-dev.fly.dev/healthz` returns 200
- [ ] `curl https://stoop-dev.fly.dev/readyz` returns 200 (DB connection works from Fly machine)
- [ ] `curl -H "Authorization: Bearer <real-JWT>" https://stoop-dev.fly.dev/v1/me` returns the landlord profile
- [ ] Sentry receives an event when triggering the debug error endpoint in production
- [ ] Fly logs visible via `fly logs` show JSON-structured output with `request_id`
- [ ] Auto-stop configured so dev app idles to zero when not used

## Out of scope

- Don't set up staging vs prod yet — `stoop-dev` is the only environment for Phase 1
- Don't add custom domain — Phase 8 when marketing site exists
- Don't configure Fly log drain to Axiom — Phase 4
- Don't enable multi-region — Phase 9+

## Effort & dependencies

- **Effort:** M (4-6 hours, mostly first-time Fly learning curve)
- **Blocks:** Phase 1 gate (along with #11, #14)
- **Blocked by:** #11, #12

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **Auto-stop vs always-on.** Dev env: auto-stop (idle to zero, ~5s cold start). Production later: always-on with min-machines = 1. For now, auto-stop saves money.

2. **VM size.** `shared-cpu-1x` with 1024MB RAM is the smallest tier and plenty for Phase 1. Bump later if needed.

3. **Concurrency.** How many requests can one machine handle? Conservative defaults are fine — Fly's `soft_limit=20, hard_limit=25` means it'll spin up another machine before saturating.

4. **Migrations on deploy.** Two options:
   - Run `alembic upgrade head` in a release_command (Fly runs it before swapping traffic)
   - Run manually from your laptop before each deploy
   - Phase 1 answer: manual is fine. release_command is brittle.

</details>

<details>
<summary><b>Hints</b></summary>

- `fly launch --no-deploy` scaffolds `fly.toml` interactively. Edit before first deploy.
- Set the region during launch: `fly launch --region yyz`
- The `[processes]` table in `fly.toml` defines what runs. For Phase 1: `web = "uvicorn app.main:app --host 0.0.0.0 --port 8080"`
- For HTTP service config: `[[services]]` with `internal_port = 8080`, `[[services.ports]]` with port 443 and handlers `["tls", "http"]`
- Health checks: `[[services.http_checks]]` with `path = "/readyz"`, `interval = "15s"`, `timeout = "5s"`
- Set secrets: `fly secrets set DATABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... -a stoop-dev`
- Verify what's set: `fly secrets list -a stoop-dev` (shows names, not values)
- Auto-stop: `auto_stop_machines = "stop"` and `auto_start_machines = true` under `[http_service]` or `[[services]]`
- View logs: `fly logs -a stoop-dev`

</details>

<details>
<summary><b>Common gotchas</b></summary>

- **Don't commit `fly.toml` with secrets.** Secrets go via `fly secrets`. Env vars in `fly.toml` are visible to anyone with repo access.
- **Use the Supabase pooler URL** (port 6543) for `DATABASE_URL` in Fly. Direct connections from a remote region get latency hits.
- **Health check path must return 200** for Fly to route traffic. If `/readyz` is returning 503, the machine is up but not serving.
- **Port mismatches.** Your app listens on 8080 (in CMD or via PORT env). `fly.toml` `internal_port` must match.
- **First deploy is slow** (~3-5 min) because Fly builds the image. Subsequent deploys are faster with layer caching.
- **`fly deploy --remote-only`** uses Fly's build cluster instead of your local Docker. Faster on slow laptops or when you're on metered internet.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my `fly.toml`:
> 1. Is the process / VM config right for a Phase 1 backend?
> 2. Are the health check intervals reasonable, or will they cause flapping?
> 3. Are auto-stop / auto-start settings right for dev?
> 4. Anything I should add now that I'll regret missing in Phase 4 when workers join?

</details>

---

**Once `curl /v1/me` returns your profile against Fly's production URL, Phase 1 is functionally complete.** Take that screencast.
