"""Shared pytest fixtures and session-level environment setup.

``app.config`` validates env vars at import time (module-level singleton).
We must set placeholder values for all *required* vars before any test
module imports ``app.config``, otherwise the import fails with a
``ValidationError`` in CI where real credentials do not exist.

The values are intentionally fake — no real Supabase/DB is contacted in
unit tests.
"""

import os

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
