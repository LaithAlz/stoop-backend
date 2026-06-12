---
title: "feat(backend): structured logging, Sentry init, and request_id middleware"
labels: ["phase-1", "type-implementation", "observability", "size-s"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

JSON-structured logs to stdout, Sentry error reporting in production, and a per-request correlation ID propagated through every log line.

## Why this matters

Without correlation IDs, debugging multi-step requests later (especially async agent processing in Phase 4) is brutal. Setting this up now means every log line is grep-friendly forever.

## Acceptance criteria

- [ ] `structlog` configured for JSON output to stdout
- [ ] Every log line includes `timestamp`, `level`, `event` (message), plus context fields
- [ ] Sentry SDK initialized only when `SENTRY_DSN` is set (so dev doesn't spam Sentry)
- [ ] Middleware reads `X-Request-ID` header (or generates a UUID if missing) and binds to logging context
- [ ] `X-Request-ID` echoed in response headers
- [ ] Every log line within a request includes `request_id`
- [ ] A test endpoint deliberately throws an exception — Sentry captures it
- [ ] A test endpoint emits a log line — visible in stdout as JSON with `request_id`
- [ ] Test endpoints gated behind `if not settings.is_production`

## Out of scope

- Don't ship logs to Axiom yet — Fly log drain happens in #13
- Don't add LangSmith tracing — Phase 3
- Don't add metrics (Prometheus/DataDog) — Phase 5+
- Don't add CORS middleware — Phase 7

## Effort & dependencies

- **Effort:** S (3-4 hours)
- **Blocks:** All future debugging
- **Blocked by:** #5, #6

---

<details>
<summary><b>Design questions to think through first</b></summary>

1. **What fields go in every log line?** Standard: timestamp, level, event. Context: request_id, request_path, request_method. Per-call: whatever the caller wants to add (landlord_id, conversation_id, etc).

2. **How does request_id propagate to async code?** Python's `contextvars` (stdlib) handles this automatically across async boundaries. structlog's `contextvars` integration reads them.

3. **Where does Sentry init live?** Could be in `main.py`, but cleaner in a separate `observability.py` module that you call from `create_app()`.

</details>

<details>
<summary><b>Hints</b></summary>

- structlog processors in this order: `merge_contextvars`, `add_log_level`, `TimeStamper(fmt="iso")`, `StackInfoRenderer`, `format_exc_info`, `JSONRenderer`
- For the middleware, subclass `starlette.middleware.base.BaseHTTPMiddleware`. Bind contextvars before `call_next`, clear after.
- structlog's `contextvars.bind_contextvars(key=value)` is what hooks into the JSON output
- Sentry SDK: `sentry_sdk.init(dsn=..., integrations=[FastApiIntegration(), StarletteIntegration()], traces_sample_rate=0.1 if production else 1.0, send_default_pii=False)`
- Use `log = structlog.get_logger()` at module level, then `log.info("event_name", key=value)` everywhere

</details>

<details>
<summary><b>Common gotchas</b></summary>

- `send_default_pii=False` is critical — without it, Sentry attaches request body / headers which includes JWTs
- Don't log `request.headers` directly. They contain the Authorization header.
- Don't use f-strings for log events: `log.info(f"user did X")`. Use `log.info("user_did_x", action=...)` — structured.
- contextvars don't propagate through `asyncio.create_task()` automatically in older Python. 3.12 is fine.
- Sentry's `traces_sample_rate=1.0` in dev sends every request — fine for dev, expensive at scale. Tune later.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my observability setup in `app/observability.py` and `app/middleware/request_id.py`:
> 1. Is structlog config correct for JSON-only output?
> 2. Will request_id actually propagate to all log lines via contextvars in async code?
> 3. Is `send_default_pii=False` enough to prevent leaking JWTs in Sentry reports?
> 4. Should I redact specific headers explicitly in Sentry?

</details>
