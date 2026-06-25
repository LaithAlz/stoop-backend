"""Shared pytest fixtures and session-level environment setup.

``app.config`` validates env vars at import time (module-level singleton).
We must set placeholder values for all *required* vars before any test
module imports ``app.config``, otherwise the import fails with a
``ValidationError`` in CI where real credentials do not exist.

The values are intentionally fake — no real Supabase/DB is contacted in
unit tests.
"""

import asyncio
import os
from collections.abc import Iterator

import pytest

# ---------------------------------------------------------------------------
# Set required env vars BEFORE any app module is imported.
# conftest.py at the tests/ root is collected by pytest before test modules,
# so this runs prior to any ``from app.config import settings``.
# ---------------------------------------------------------------------------
_PLACEHOLDER_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_JWKS_URL": "https://test.supabase.co/auth/v1/.well-known/jwks.json",
    "SUPABASE_JWT_ISSUER": "https://test.supabase.co/auth/v1",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
}

for _key, _value in _PLACEHOLDER_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _reset_jwks_auth_state() -> Iterator[None]:
    """Reset the JWKS cache AND its asyncio.Lock before every test, globally.

    ``supabase_auth`` caches the JWKS in a module-level dict guarded by a
    module-level ``asyncio.Lock`` created at import time. With
    ``asyncio_default_fixture_loop_scope=function`` each test runs in its own
    event loop; reusing the same Lock object across loops can raise
    ``RuntimeError: ... attached to a different loop`` inside ``_get_jwks``,
    which ``verify_jwt`` catches and surfaces as a 401 — an intermittent,
    order-dependent flake (seen on a slow CI runner, not locally).

    Resetting both the cache and the lock here, for ALL test files, makes JWT
    verification deterministic regardless of test order. Imported lazily so it
    runs after the placeholder env above is set.
    """
    import app.integrations.supabase_auth as auth_mod

    auth_mod._jwks_cache = None  # noqa: SLF001
    auth_mod._jwks_lock = asyncio.Lock()  # noqa: SLF001
    yield
