"""Stoop API — centralised environment / settings.

All environment variables for Phase 1 are declared here as typed,
validated Pydantic fields.  A missing required variable raises
``pydantic.ValidationError`` at startup — no silent runtime surprises.

Usage everywhere else::

    from app.config import settings

    if settings.is_production:
        ...

IMPORTANT: Never log or print the ``settings`` object — it carries secrets.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Phase 1 environment variables, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Runtime environment
    # ------------------------------------------------------------------

    environment: Literal["dev", "staging", "production"] = "dev"
    """Which deployment environment we're running in.

    Defaults to ``"dev"`` so local and test imports work without setting
    ``ENVIRONMENT``.  Any other value is a startup error (typo protection).
    """

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    """Minimum log level passed to structlog / stdlib logging."""

    # ------------------------------------------------------------------
    # Database (sensitive — no defaults)
    # ------------------------------------------------------------------

    database_url: str = Field(
        ...,
        description=(
            "Async SQLAlchemy connection string, e.g. "
            "postgresql+asyncpg://USER:PASSWORD@HOST:6543/postgres"
        ),
    )

    app_database_url: str | None = Field(
        default=None,
        description=(
            "Optional SECOND connection string for REQUEST-path sessions "
            "(app/db/session.py's get_session), using the app_role Postgres "
            "login that migration 0005's RLS policies key off (#22). Same "
            "Supavisor pooler host as DATABASE_URL, different user/password "
            "-- e.g. postgresql+asyncpg://app_role.PROJECT_REF:PASSWORD@HOST:6543/postgres. "
            "Leave unset (default: local dev, CI, and production until the "
            "one-time operator step in app/db/session.py's module docstring "
            "is done) -- request sessions then fall back to the admin "
            "engine (DATABASE_URL) and a one-time startup WARNING notes "
            "that RLS is not yet enforced by role separation. app_role has "
            "NO password until an operator sets one directly against the "
            "database (never in a migration, never here). REQUIRED when "
            "ENVIRONMENT=production -- see _require_app_database_url_in_production."
        ),
    )

    # ------------------------------------------------------------------
    # Supabase (sensitive — no defaults)
    # ------------------------------------------------------------------

    supabase_url: str = Field(
        ...,
        description="Base URL of the Supabase project, e.g. https://xyz.supabase.co",
    )

    supabase_jwks_url: str = Field(
        ...,
        description=(
            "JWKS endpoint used for JWT verification, e.g. "
            "https://xyz.supabase.co/auth/v1/.well-known/jwks.json"
        ),
    )

    supabase_jwt_issuer: str = Field(
        ...,
        description=("Expected 'iss' claim in Supabase JWTs, e.g. https://xyz.supabase.co/auth/v1"),
    )

    supabase_service_role_key: str = Field(
        ...,
        description="Supabase service-role key (Fly secret — never expose to clients).",
    )

    # ------------------------------------------------------------------
    # Twilio (webhook signature verification — #40/#152; sensitive)
    # ------------------------------------------------------------------

    twilio_auth_token: str = Field(
        ...,
        description=(
            "Twilio Auth Token used to verify X-Twilio-Signature on inbound "
            "webhooks (app/integrations/twilio.py). NEVER logged, NEVER "
            "included in any error message. Required — a real Twilio "
            "account/number already exists for this project (see .env); "
            "unlike app_database_url this has no safe fallback because "
            "there is no way to verify a webhook signature without it."
        ),
    )

    twilio_account_sid: str = Field(
        ...,
        description=(
            "Twilio Account SID — paired with twilio_auth_token to construct "
            "the outbound REST client (app/integrations/twilio_send.py, #108). "
            "Used ONLY by the emergency escalation chain today (the other "
            "sanctioned sender, the approve-flow draft sender, is #44, "
            "unbuilt). NEVER logged, NEVER included in any error message. "
            "Required — the same real Twilio account referenced by "
            "twilio_auth_token already has this SID (see .env)."
        ),
    )

    public_base_url: str | None = Field(
        default=None,
        description=(
            "The public HTTPS origin Twilio is configured to POST webhooks "
            "to, e.g. https://api.stoop.example — used to reconstruct the "
            "EXACT url Twilio signed, proxy-aware. When set, the signed url "
            "is public_base_url + request path (+ query). When unset "
            "(local dev default), app/integrations/twilio.py falls back to "
            "request.url, honoring X-Forwarded-Proto/X-Forwarded-Host from "
            "the trusted proxy hop (Fly.io terminates TLS at its edge — see "
            "that module's reconstruct_signing_url docstring). REQUIRED when "
            "ENVIRONMENT=production -- see _require_public_base_url_in_production: "
            "signature verification must not depend on trusting proxy headers "
            "in production."
        ),
    )

    # ------------------------------------------------------------------
    # Anthropic (agent — #26/#9+; sensitive — no default)
    # ------------------------------------------------------------------

    anthropic_api_key: str = Field(
        ...,
        description=(
            "Anthropic API key used by the agent's classify_severity/"
            "draft_response nodes (app/integrations/anthropic.py, lands "
            "with #9+). Required -- a real key already exists for this "
            "project (see .env). NEVER logged, NEVER included in any error "
            "message."
        ),
    )

    # ------------------------------------------------------------------
    # LangSmith (agent tracing — #26; optional, like sentry_dsn)
    # ------------------------------------------------------------------

    langsmith_api_key: str | None = Field(
        default=None,
        description=(
            "LangSmith API key for LangGraph/LangChain tracing (#26). "
            "Leave unset to disable tracing entirely -- there is no "
            "LangSmith account yet. When set, "
            "app/observability.py's init_langsmith_tracing() exports the "
            "LANGSMITH_TRACING/LANGSMITH_API_KEY/LANGSMITH_PROJECT env "
            "vars the langsmith SDK reads ambiently; when unset, none of "
            "those env vars are ever exported and nothing about tracing "
            "is attempted -- a missing/absent LangSmith account must "
            "never break app startup or agent runs."
        ),
    )

    langsmith_project: str | None = Field(
        default=None,
        description=(
            "LangSmith project name traces are grouped under (#26). Only "
            "meaningful when langsmith_api_key is set; the langsmith SDK "
            "falls back to its own 'default' project when unset."
        ),
    )

    # ------------------------------------------------------------------
    # Observability (optional)
    # ------------------------------------------------------------------

    sentry_dsn: str | None = None
    """Sentry DSN.  Leave unset (or blank) to disable Sentry entirely."""

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        """True when running in the production environment.

        Referenced by issue #7 (logging / Sentry wiring) and beyond.
        """
        return self.environment == "production"

    # ------------------------------------------------------------------
    # Production boot gate (#22 safety review items 3 and 13a)
    # ------------------------------------------------------------------

    @field_validator("app_database_url", mode="after")
    @classmethod
    def _normalize_app_database_url(cls, v: str | None) -> str | None:
        """Treat a whitespace-only value the same as unset (#22 safety
        review item 13a).

        A blank/placeholder Fly secret (``APP_DATABASE_URL="   "``, or one
        accidentally set to the empty string) would otherwise be truthy
        (a non-empty Python string) and sail past the boot gate below,
        then fail later at ``create_async_engine`` with an obscure parse
        error instead of this module's clear, intentional message.
        Normalizing here means every consumer of ``settings.app_database_url``
        (this boot gate, ``app/db/session.py``'s fallback branch, the
        startup role-separation self-check) sees a single consistent
        "is it actually set" signal, instead of each having to re-implement
        the same ``.strip()`` check.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_app_database_url_in_production(self) -> Settings:
        """Refuse to boot in production without RLS role separation (#22).

        ``app_database_url`` unset is a deliberately safe DEFAULT for local
        dev/CI/production-before-the-operator-step (see
        ``app/db/session.py``'s module docstring) — request sessions fall
        back to the admin engine and only a WARNING is logged. That
        fallback is fine right up until real tenant data exists. It is NOT
        an acceptable steady state for a production boot: once
        ``ENVIRONMENT=production`` is set, this refuses to start at all
        rather than silently run every request through the admin engine,
        unscoped by RLS, indefinitely. No secrets in this message — it
        only ever fires because a value is ABSENT (the field validator
        above already normalized a whitespace-only value to ``None``, so
        this check sees the same "unset" either way).
        """
        if self.environment == "production" and not self.app_database_url:
            raise ValueError(
                "APP_DATABASE_URL is required when ENVIRONMENT=production "
                "(RLS role separation, #22) -- refusing to boot without it. "
                "See app/db/session.py's module docstring for the one-time "
                "operator step (ALTER ROLE app_role LOGIN PASSWORD ...; "
                "then set the APP_DATABASE_URL Fly secret)."
            )
        return self

    # ------------------------------------------------------------------
    # Production boot gate (#40/#152 consolidated review item 5) --
    # mirrors _normalize_app_database_url / _require_app_database_url_in_
    # production exactly, same rationale, different field.
    # ------------------------------------------------------------------

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _normalize_public_base_url(cls, v: str | None) -> str | None:
        """Treat a whitespace-only value the same as unset — same reasoning
        as ``_normalize_app_database_url`` above (a blank/placeholder Fly
        secret must not silently sail past the boot gate below)."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_public_base_url_in_production(self) -> Settings:
        """Refuse to boot in production without a configured
        ``public_base_url`` (#40/#152 consolidated safety review).

        ``app/integrations/twilio.py``'s ``reconstruct_signing_url`` falls
        back to trusting ``X-Forwarded-Proto``/``X-Forwarded-Host`` request
        headers when ``public_base_url`` is unset — safe ONLY because
        Fly.io is the single, trusted proxy hop in front of this app today.
        That fallback is a reasonable DEFAULT for local dev (see
        ``public_base_url``'s field description) but is NOT an acceptable
        steady state for a production boot: Twilio signature verification
        (the only thing standing between this webhook and an unauthenticated
        caller) must not depend on trusting proxy headers whose provenance
        this config layer cannot itself verify. Mirrors
        ``_require_app_database_url_in_production``'s precedent exactly.
        """
        if self.environment == "production" and not self.public_base_url:
            raise ValueError(
                "PUBLIC_BASE_URL is required when ENVIRONMENT=production "
                "(#40/#152) -- refusing to boot without it. Twilio signature "
                "verification must not depend on trusting proxy headers in "
                "production; set PUBLIC_BASE_URL to the public HTTPS origin "
                "Twilio is configured to POST webhooks to."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached ``Settings`` singleton.

    The ``@lru_cache`` ensures env vars are read exactly once per process.
    Tests can call ``get_settings.cache_clear()`` to force a fresh read.
    """
    return Settings()


# Module-level singleton — imported by the rest of the application.
# Constructed once at import time; a missing required var raises
# ``pydantic.ValidationError`` immediately (fast-fail at startup).
settings: Settings = get_settings()
