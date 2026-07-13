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

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Twilio Account SIDs are always "AC" + 32 lowercase-hex characters (34
# chars total) -- production-only shape gate, see
# Settings._require_valid_twilio_account_sid_format_in_production below.
_TWILIO_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")


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

    twilio_messaging_service_sid: str | None = Field(
        default=None,
        description=(
            "Optional Twilio Messaging Service SID for automatic A2P 10DLC/"
            "CASL campaign association on newly-provisioned property numbers "
            "(app/property_provisioning.py, #53). Unset today -- A2P "
            "registration is still pending externally (architecture.md: "
            "'a milestone-1 task, not an afterthought'). When unset, "
            "POST /v1/properties still provisions a fully working number, "
            "just without the campaign association -- logged and skipped "
            "gracefully, never failing provisioning on it. Set this the day "
            "a real Messaging Service + campaign exists to start associating "
            "every NEWLY provisioned number automatically; it has no effect "
            "on numbers provisioned before it was set (no retroactive "
            "backfill)."
        ),
    )

    max_properties_per_landlord: int = Field(
        default=25,
        description=(
            "Hard cap on how many properties (and therefore live, "
            "real-money Twilio numbers) a single landlord can provision "
            "(app/routers/properties.py, #53 safety review finding H1). "
            "Checked BEFORE any Twilio call, purely as a guard against "
            "unbounded spend from a buggy or malicious client hammering "
            "POST /v1/properties -- this is NOT an entitlement/paywall "
            "gate (never-break rule #1: the emergency line is never "
            "paywalled or throttled) -- every landlord, free or paid, "
            "gets the identical cap. 25 is a generous ceiling for a v1 "
            "self-serve landlord; raise the default here (never via a "
            "feature flag -- this is a cost/safety guard, not a rollout "
            "knob or pricing cohort) if a real landlord ever needs more."
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

    # ------------------------------------------------------------------
    # Production boot gate (#108 safety review, 2026-07-12, finding 7) --
    # mirrors _require_public_base_url_in_production's PATTERN: a strict
    # SHAPE check gated to production only, so dev/test placeholder values
    # (which need not look like a real Twilio SID) keep working unchanged.
    # ------------------------------------------------------------------

    @field_validator("twilio_account_sid", mode="after")
    @classmethod
    def _normalize_twilio_account_sid(cls, v: str) -> str:
        """Reject a blank/whitespace-only value in EVERY environment (this
        field has no safe "unset" fallback the way ``app_database_url``/
        ``public_base_url`` do -- there is no code path that works without
        a real Account SID once the outbound Twilio client is ever
        constructed), while still allowing loosely-shaped dev/test
        placeholders through -- the STRICT shape check below is
        production-only. Mirrors ``_normalize_app_database_url``/
        ``_normalize_public_base_url``'s "a blank Fly secret must not
        silently sail past validation" rationale.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "TWILIO_ACCOUNT_SID must not be blank -- required to construct "
                "the outbound Twilio client (app/integrations/twilio_send.py, #108)."
            )
        return stripped

    @model_validator(mode="after")
    def _require_valid_twilio_account_sid_format_in_production(self) -> Settings:
        """Refuse to boot in production unless ``twilio_account_sid`` is
        non-empty AND shaped like a real Twilio Account SID (``AC`` +
        32 hex characters, 34 characters total) — safety review, 2026-07-12,
        finding 7. A misconfigured/placeholder value (e.g. copy-pasted from
        ``.env.example``, or truncated) would otherwise sail past startup
        and only surface as a confusing 401 from Twilio's API the first
        time the emergency chain tries to place a real call or send a real
        SMS — precisely the worst moment to discover a config typo. Dev/
        test keep using loosely-shaped placeholders unchanged (this check
        is production-only, mirroring
        ``_require_public_base_url_in_production``'s exact gating pattern).
        No secrets in this message — the SID itself is not secret (it is
        Twilio's public account identifier, always sent in the clear as
        part of the REST URL/Basic-Auth username), but is still never
        echoed here on principle (same discipline as every other boot-gate
        message in this class).
        """
        if self.environment != "production":
            return self
        sid = self.twilio_account_sid
        if not _TWILIO_ACCOUNT_SID_RE.match(sid):
            raise ValueError(
                "TWILIO_ACCOUNT_SID does not look like a real Twilio Account SID "
                "(expected 'AC' followed by 32 hex characters) -- refusing to boot "
                "in production with what looks like a placeholder or typo'd value."
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
