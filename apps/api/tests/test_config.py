"""Tests for app.config — Settings validation and singleton behaviour."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings, settings


@pytest.mark.unit
def test_settings_singleton_is_cached() -> None:
    """get_settings() returns the same object on every call."""
    a = get_settings()
    b = get_settings()
    assert a is b


@pytest.mark.unit
def test_module_level_settings_is_same_as_get_settings() -> None:
    """The module-level ``settings`` alias is identical to get_settings()."""
    assert settings is get_settings()


@pytest.mark.unit
def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """environment and log_level have sensible defaults."""
    # Remove optional overrides so we test the class defaults.
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    # Construct directly (bypasses module-level singleton) with _env_file=None
    # so pydantic-settings does not load a .env file from disk.
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
    )
    assert s.environment == "dev"
    assert s.log_level == "INFO"
    assert s.sentry_dsn is None
    assert s.is_production is False


@pytest.mark.unit
def test_is_production_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_production returns True only for environment='production'."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    s_prod = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
    )
    assert s_prod.is_production is True

    s_dev = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="dev",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
    )
    assert s_dev.is_production is False


@pytest.mark.unit
def test_invalid_environment_literal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad value for ``environment`` fails validation immediately."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="local",  # type: ignore[arg-type]  # not a valid Literal
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
        )


@pytest.mark.unit
def test_missing_required_vars_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required env vars cause a ValidationError at construction time.

    This is the key acceptance criterion: misconfiguration fails loudly at
    startup, not silently at the first runtime call that needs a value.
    """
    # Clear every required var from the environment so the Settings class
    # cannot fall back to them.
    required_vars = [
        "DATABASE_URL",
        "SUPABASE_URL",
        "SUPABASE_JWKS_URL",
        "SUPABASE_JWT_ISSUER",
        "SUPABASE_SERVICE_ROLE_KEY",
    ]
    for var in required_vars:
        monkeypatch.delenv(var, raising=False)

    # Construct directly with _env_file=None so no .env file is loaded.
    # No keyword args for required fields → ValidationError must be raised.
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors = exc_info.value.errors()
    missing_fields = {e["loc"][0] for e in errors if e["type"] == "missing"}
    assert "database_url" in missing_fields
    assert "supabase_url" in missing_fields
    assert "supabase_service_role_key" in missing_fields
