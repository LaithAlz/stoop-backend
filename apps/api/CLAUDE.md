# apps/api — FastAPI + LangGraph backend

Read the root `CLAUDE.md` first. This file adds API-specific conventions.
The app skeleton is built by issues #1–#16; layout below is the target
(`docs/plans/architecture.md` §4 is the authority).

## Stack & commands

Python 3.12 · FastAPI · async SQLAlchemy 2.0 · Pydantic v2 · Alembic ·
LangGraph + Anthropic SDK · structlog · uv.

```bash
uv sync                                   # deps
uv run uvicorn app.main:app --reload      # run (needs .env, see .env.example)
uv run pytest                             # unit + integration (no network)
uv run pytest -m eval                     # evals — REAL Anthropic API, costs money
uv run alembic upgrade head               # migrate (down/up must round-trip)
uv run ruff check . && uv run mypy app    # lint + types (strict)
```

## Layout (target)

```
app/
  main.py settings.py deps.py
  db/         engine, session, migrations/
  models/     SQLAlchemy models — names from docs/plans/schema-v1.md
  routers/    me, properties, tenants, vendors, queue, cases, drafts,
              notifications, billing, webhooks/{twilio,stripe}
  agent/      graph.py, state.py, rubric.py, prefilter.py, tools.py,
              prompts/v1.py, nodes/{identify_property, load_context,
              identify_case, classify_intent, classify_severity,
              draft_response, emergency_protocol}
  integrations/  supabase_auth.py, twilio.py, anthropic.py, weather.py, posthog.py
evals/        scenarios/*.yaml + runner (format: docs/plans/eval-scenarios-v1.md)
```

## Conventions

- **Contracts:** every endpoint matches `docs/plans/api-contracts.md` —
  error envelope `{"error": {"code", "message", "request_id"}}`, cursor
  pagination, ISO-8601 UTC. New/changed endpoint ⇒ update that doc in the
  same PR.
- **DB:** async sessions only; `text+CHECK` not enums; money in
  `numeric` cents; upserts (`ON CONFLICT`) over select-then-insert;
  every multi-tenant query scoped by `landlord_id` (RLS arrives in #22,
  code behaves as if it's already on).
- **Auth:** JWKS asymmetric verification only (#10). Accept only
  `role: "authenticated"`. Never the legacy shared secret. Never log tokens.
- **Twilio webhooks:** verify signature → persist message → return 200 →
  process in background. Dedupe on `twilio_sid`. Never lose a message.
- **Structlog:** bind `request_id` always, `landlord_id`/`case_id` where
  known. No PII, no message bodies, no phone numbers in logs.
- **Typing:** mypy strict passes; Pydantic models for every boundary
  (request, response, tool output, agent state).

## Agent rules (the load-bearing ones)

- `rubric.py` is **byte-identical** to the verbatim block in
  `docs/plans/severity-rubric-v1.md` — a checksum test enforces it. To
  change behavior: new rubric version + new prompt file + full eval run.
- Prompts live in `prompts/v{n}.py`, frozen — never edit an existing
  version, add `v{n+1}`.
- Every node appends a human-readable line to `state.reasoning_log` —
  it is shown to landlords on the approval card, not just debugging.
- `classify_severity` calls the Anthropic SDK directly, temperature 0,
  output validated by Pydantic; record tokens/cost on the message row.
- The Tier-0 prefilter (`prefilter.py`, pure functions, no I/O) runs in
  the webhook handler **before** the graph. The agent may escalate past a
  Tier-0 miss; it may never de-escalate a Tier-0 fire.
- 20 s classification budget → one retry → degraded mode
  (`docs/plans/emergency-prefilter.md`): holding ack + landlord
  notification. No silent failures.
- One pending draft per case (partial unique index); new inbound on a
  pending case marks the draft `stale` and re-runs from `load_context`.
- Approve = set `scheduled_send_at = now()+5s`; the sender only sends
  `approved` rows whose time has come (the undo window is data).
- Send to tenant/vendor happens **only** through the draft flow or the
  emergency safety path. There is no other code path that calls
  `twilio.send`. Keep it that way.
- Feature flags (PostHog, server-side evaluation with local fallback)
  gate rollouts and pricing cohorts only — **never** the emergency path,
  rubric, or approval requirements. Flag-service failure must be
  indistinguishable from flags-off.

## Testing

- Markers: `unit` (default), `integration` (DB via docker-compose),
  `eval` (real API, never in default runs, in CI only for prompt/rubric/
  eval changes — #73).
- Eval scoring per `eval-scenarios-v1.md`: 3 samples, temp 0, flaky = fail;
  E-class/F-class failures block merge.
- New production misclassification ⇒ new eval YAML in the same week.
- RLS isolation tests (#23) must cover every table in `schema-v1.md`.

## Things humans must do (don't attempt, ask)

Supabase/Twilio/LangSmith/Sentry/Fly account creation, secrets
(`fly secrets set`), A2P/CASL filings, Stripe dashboard products, DNS.
If a task needs credentials that don't exist, stop and say so.
