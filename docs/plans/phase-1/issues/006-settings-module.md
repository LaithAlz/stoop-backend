---
title: "feat(backend): settings module with pydantic-settings env validation"
labels: ["phase-1", "type-implementation", "size-xs"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Centralize environment variable access through a typed, validated Pydantic settings object.

## Why this matters

Scattered `os.getenv()` calls are fragile — missing vars fail at runtime, types aren't checked, defaults inconsistent. One settings module catches misconfiguration at startup.

## Acceptance criteria

- [ ] `backend/app/config.py` defines a `Settings` class extending `pydantic_settings.BaseSettings`
- [ ] All Phase 1 env vars defined with correct types (use `Literal` types where appropriate)
- [ ] Missing required env vars cause a clear startup error (not silent failure or runtime crash)
- [ ] Module exposes a `settings` singleton — cached, not re-read on every access
- [ ] `.env` file in `backend/` loads automatically in dev (gitignored)
- [ ] `.env.example` lists every required var with placeholder values
- [ ] All other modules import `from app.config import settings` — no direct env reads elsewhere
- [ ] One test verifies that missing required vars raises a `ValidationError`

## Out of scope

- Don't add env vars for things not used in Phase 1 (Inngest, Twilio, Anthropic, etc.) — add them as you need them in their respective phases
- Don't build a settings UI / admin endpoint
- Don't add feature flags

## Effort & dependencies

- **Effort:** XS (1-2 hours)
- **Blocks:** #7, #9, #10
- **Blocked by:** #5

---

<details>
<summary><b>Design questions to think through first</b></summary>

By hand, list every env var your Phase 1 app needs. For each:
- Required at startup, or optional with default?
- Sensitive (Fly secret) or non-sensitive (regular env var)?
- Type — `str`, `int`, `bool`, `Literal[...]`, `list[str]`?

Phase 1 vars: environment, log_level, database_url, supabase_url, supabase_jwks_url, supabase_jwt_issuer, supabase_service_role_key, sentry_dsn (optional).

</details>

<details>
<summary><b>Hints</b></summary>

- `pydantic_settings.SettingsConfigDict` configures env file loading: `env_file=".env"`, `case_sensitive=False`, `extra="ignore"`
- Use `Literal["dev", "staging", "production"]` for environment instead of plain str — catches typos at startup
- Use `Field(..., description="...")` for required fields with no default — the ellipsis makes them required
- For the singleton, `@lru_cache(maxsize=1)` on a `get_settings()` function and a module-level `settings = get_settings()` is the cleanest pattern
- `extra="ignore"` lets you add new env vars to `.env` for experimentation without breaking startup

</details>

<details>
<summary><b>Common gotchas</b></summary>

- Don't give sensitive vars (DATABASE_URL, secrets) default values. Required fields should fail loudly.
- Don't `print(settings)` in production logs — it dumps secrets
- mypy will complain about `Settings()` with no args — you may need `# type: ignore[call-arg]` or rely on the Pydantic plugin
- `extra="forbid"` is more strict but will bite you when you add new env vars and forget to update something. `"ignore"` is more practical solo.

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review `app/config.py`:
> 1. Are field types tight enough (`Literal`s where possible)?
> 2. Any sensitive vars accidentally given defaults?
> 3. Is the singleton pattern correct for testability?
> 4. Should I be validating URLs at startup (DATABASE_URL format, JWKS URL reachable)?

</details>
