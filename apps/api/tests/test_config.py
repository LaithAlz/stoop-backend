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
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.environment == "dev"
    assert s.log_level == "INFO"
    assert s.sentry_dsn is None
    assert s.is_production is False


@pytest.mark.unit
def test_app_database_url_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``app_database_url`` (#22) is optional — unset by default, and its
    absence must not break Settings construction (local dev/CI/production
    before the one-time operator step in app/db/session.py's docstring)."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.app_database_url is None


@pytest.mark.unit
def test_app_database_url_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When set, ``app_database_url`` is carried through unmodified."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.app_database_url == "postgresql+asyncpg://app_role:secret@h:6543/db"


@pytest.mark.unit
@pytest.mark.parametrize("blank_value", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab_newline"])
def test_app_database_url_whitespace_only_normalizes_to_none(
    monkeypatch: pytest.MonkeyPatch, blank_value: str
) -> None:
    """A whitespace-only (or empty-string) APP_DATABASE_URL must normalize
    to None (#22 safety review item 13a) — treated identically to unset,
    not as a truthy-but-garbage value that would sail past the production
    boot gate and fail later at create_async_engine with an obscure
    parse error instead."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        app_database_url=blank_value,
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.app_database_url is None


@pytest.mark.unit
def test_production_with_whitespace_only_app_database_url_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boot gate must fire for a whitespace-only APP_DATABASE_URL in
    production, exactly as if it were unset (#22 safety review item 13a)."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            app_database_url="   ",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )

    assert "APP_DATABASE_URL" in str(exc_info.value)


@pytest.mark.unit
def test_production_without_app_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot gate (#22 safety review item 3): ENVIRONMENT=production with no
    APP_DATABASE_URL must refuse to construct Settings at all, not silently
    fall back to the admin engine for every request forever."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )

    message = str(exc_info.value)
    assert "APP_DATABASE_URL" in message
    # No secrets ever appear in this message — it only fires on absence.
    assert "postgresql" not in message.lower()


@pytest.mark.unit
def test_production_with_app_database_url_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot gate does not fire once APP_DATABASE_URL is actually set."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        # Required alongside environment="production" -- the #40/#152 boot
        # gate (see test_production_without_public_base_url_raises) would
        # otherwise refuse to construct this Settings instance at all.
        public_base_url="https://api.stoop.example",
    )
    assert s.is_production is True
    assert s.app_database_url == "postgresql+asyncpg://app_role:secret@h:6543/db"


@pytest.mark.unit
def test_non_production_without_app_database_url_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot gate is production-only — dev/staging without
    APP_DATABASE_URL must NOT raise (the documented fallback behavior)."""
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)

    for env in ("dev", "staging"):
        s = Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment=env,  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )
        assert s.app_database_url is None


@pytest.mark.unit
def test_is_production_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_production returns True only for environment='production'."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    s_prod = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        # Required alongside environment="production" — the #22 boot gate
        # (see test_production_without_app_database_url_raises) would
        # otherwise refuse to construct this Settings instance at all.
        app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        # Required alongside environment="production" -- see the
        # public_base_url boot-gate tests below.
        public_base_url="https://api.stoop.example",
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
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s_dev.is_production is False


# ---------------------------------------------------------------------------
# public_base_url production boot gate (#40/#152 consolidated review item 5)
# -- mirrors the app_database_url gate tests above exactly, different field.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_without_public_base_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENVIRONMENT=production with APP_DATABASE_URL set but no
    PUBLIC_BASE_URL must refuse to construct Settings at all -- signature
    verification must not depend on trusting proxy headers in production."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )

    message = str(exc_info.value)
    assert "PUBLIC_BASE_URL" in message


@pytest.mark.unit
def test_production_with_whitespace_only_public_base_url_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only PUBLIC_BASE_URL must be treated exactly like unset
    (same reasoning as the app_database_url gate's 13a fix)."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
            public_base_url="   ",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )

    assert "PUBLIC_BASE_URL" in str(exc_info.value)


@pytest.mark.unit
def test_production_with_public_base_url_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot gate does not fire once PUBLIC_BASE_URL is actually set."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
        public_base_url="https://api.stoop.example",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.is_production is True
    assert s.public_base_url == "https://api.stoop.example"


@pytest.mark.unit
def test_non_production_without_public_base_url_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot gate is production-only -- dev/staging without
    PUBLIC_BASE_URL must NOT raise (the documented local-dev fallback)."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    for env in ("dev", "staging"):
        s = Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment=env,  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )
        assert s.public_base_url is None


@pytest.mark.unit
@pytest.mark.parametrize("blank_value", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab_newline"])
def test_public_base_url_whitespace_only_normalizes_to_none(
    monkeypatch: pytest.MonkeyPatch, blank_value: str
) -> None:
    """Same normalization as app_database_url's 13a fix, applied to
    public_base_url."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        public_base_url=blank_value,
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
    )
    assert s.public_base_url is None


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
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
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


# ---------------------------------------------------------------------------
# anthropic_api_key (#26) — required, like twilio_auth_token/supabase_*.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anthropic_api_key_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing ANTHROPIC_API_KEY fails loudly at startup, same as the other
    required credentials (#26)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        )

    errors = exc_info.value.errors()
    missing_fields = {e["loc"][0] for e in errors if e["type"] == "missing"}
    assert "anthropic_api_key" in missing_fields


@pytest.mark.unit
def test_anthropic_api_key_is_carried_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        anthropic_api_key="sk-ant-test",  # noqa: S106
    )
    assert s.anthropic_api_key == "sk-ant-test"


# ---------------------------------------------------------------------------
# langsmith_api_key / langsmith_project (#26) — optional, like sentry_dsn.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_langsmith_fields_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LangSmith account yet -- both fields must be optional and default
    to None so Settings construction never fails on their absence."""
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        anthropic_api_key="sk-ant-test",  # noqa: S106
    )
    assert s.langsmith_api_key is None
    assert s.langsmith_project is None


@pytest.mark.unit
def test_langsmith_fields_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        anthropic_api_key="sk-ant-test",  # noqa: S106
        langsmith_api_key="ls-test-key",  # noqa: S106
        langsmith_project="stoop-dev",
    )
    assert s.langsmith_api_key == "ls-test-key"
    assert s.langsmith_project == "stoop-dev"


# ---------------------------------------------------------------------------
# twilio_account_sid (safety review, 2026-07-12, finding 7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("blank_value", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab_newline"])
def test_twilio_account_sid_blank_always_raises(
    monkeypatch: pytest.MonkeyPatch, blank_value: str
) -> None:
    """A blank Account SID has no safe "unset" fallback -- unlike
    app_database_url/public_base_url, this field is never optional, so a
    blank value must raise in EVERY environment, not just production."""
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
            twilio_account_sid=blank_value,  # noqa: S106
        )
    assert "TWILIO_ACCOUNT_SID" in str(exc_info.value)


@pytest.mark.unit
def test_twilio_account_sid_whitespace_is_stripped() -> None:
    """Surrounding whitespace on an otherwise-valid value is trimmed, same
    normalization convention as the other boot-gated fields."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        twilio_account_sid="  AC" + "1" * 32 + "  ",  # noqa: S106
    )
    assert s.twilio_account_sid == "AC" + "1" * 32


@pytest.mark.unit
def test_production_with_malformed_twilio_account_sid_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty but wrongly-shaped SID (typo, .env.example placeholder,
    truncated value) must refuse to boot in production -- catching a
    config mistake at startup, not on the first real Twilio call."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
            public_base_url="https://api.stoop.example",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
            twilio_account_sid="your-twilio-account-sid-here",  # noqa: S106
        )
    assert "TWILIO_ACCOUNT_SID" in str(exc_info.value)


@pytest.mark.unit
def test_production_with_valid_twilio_account_sid_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        app_database_url="postgresql+asyncpg://app_role:secret@h:6543/db",
        public_base_url="https://api.stoop.example",
        supabase_url="https://x.supabase.co",
        supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
        supabase_jwt_issuer="https://x.supabase.co/auth/v1",
        supabase_service_role_key="key",
        twilio_auth_token="test-twilio-auth-token",  # noqa: S106
        twilio_account_sid="AC" + "0" * 32,  # noqa: S106
    )
    assert s.twilio_account_sid == "AC" + "0" * 32


@pytest.mark.unit
def test_non_production_with_malformed_twilio_account_sid_is_fine() -> None:
    """The shape gate is production-only -- dev/test placeholders that
    don't look like a real Twilio SID must keep working unchanged."""
    for env in ("dev", "staging"):
        s = Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment=env,  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:p@h:5432/db",
            supabase_url="https://x.supabase.co",
            supabase_jwks_url="https://x.supabase.co/auth/v1/.well-known/jwks.json",
            supabase_jwt_issuer="https://x.supabase.co/auth/v1",
            supabase_service_role_key="key",
            twilio_auth_token="test-twilio-auth-token",  # noqa: S106
            twilio_account_sid="not-a-real-sid",  # noqa: S106
        )
        assert s.twilio_account_sid == "not-a-real-sid"
