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

from pydantic import Field
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
