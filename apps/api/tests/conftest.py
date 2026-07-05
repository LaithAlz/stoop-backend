"""Shared pytest fixtures and session-level environment setup.

``app.config`` validates env vars at import time (module-level singleton).
We must set placeholder values for all *required* vars before any test
module imports ``app.config``, otherwise the import fails with a
``ValidationError`` in CI where real credentials do not exist.

The values are intentionally fake — no real Supabase/DB is contacted in
unit tests.
"""

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
    "TWILIO_AUTH_TOKEN": "test-twilio-auth-token",
    "ANTHROPIC_API_KEY": "test-anthropic-api-key",
}

for _key, _value in _PLACEHOLDER_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _reset_jwks_auth_state() -> Iterator[None]:
    """Reset the JWKS cache and all its rate-limit/cooldown state before
    every test, globally.

    ``supabase_auth`` consolidates its JWKS cache and bookkeeping into a
    single ``_JwksState`` instance (issue #158 follow-up refactor), which
    provides ``reset_for_tests()`` for exactly this purpose — including
    replacing the ``asyncio.Lock`` with a fresh one. That matters because
    with ``asyncio_default_fixture_loop_scope=function`` each test runs in
    its own event loop; reusing the same Lock object across loops can raise
    ``RuntimeError: ... attached to a different loop`` inside ``_get_jwks``,
    which ``verify_jwt`` catches and surfaces as a 401 — an intermittent,
    order-dependent flake (seen on a slow CI runner, not locally).

    Resetting this here, for ALL test files, makes JWT verification
    deterministic regardless of test order. Imported lazily so it runs
    after the placeholder env above is set.

    This also covers the forced-refresh rate-limit stamp (kid-miss path,
    #134) and the degenerate-fetch and fetch-exception cooldown stamps
    (routine path, #147 follow-up and #158) — otherwise a test that trips
    any of those leaves its timestamp set, and a later test within the same
    window would see its own attempt silently skipped, causing an
    order-dependent flake (the same class of bug that caused #141).
    """
    import app.integrations.supabase_auth as auth_mod

    auth_mod._jwks_state.reset_for_tests()  # noqa: SLF001
    yield


@pytest.fixture(autouse=True)
def _reset_weather_cache() -> Iterator[None]:
    """Reset the weather integration's module-level TTL cache before every
    test — same cross-test-leakage rationale as ``_reset_jwks_auth_state``
    above: tests reuse the same handful of synthetic lat/lon coordinates
    (e.g. Toronto's), so a cache entry populated by an earlier test would
    otherwise make a later test that expects a fresh fetch silently observe
    a cache hit instead — an order-dependent flake.
    """
    import app.integrations.weather as weather_mod

    weather_mod._cache_state.reset_for_tests()  # noqa: SLF001
    yield


@pytest.fixture(autouse=True)
def _reset_checkpointer_pool() -> Iterator[None]:
    """Forget the checkpointer's module-global psycopg pool between tests.

    Same failure class as ``_reset_jwks_auth_state`` above (#141): the
    ``AsyncConnectionPool`` (internal ``asyncio.Lock`` + background worker
    tasks) binds to the event loop that opened it, and each test runs its
    own loop. A pool surviving across tests is a latent cross-loop flake —
    caught by the PR #172 senior review before it fired. We drop the
    reference (loop-independent) rather than awaiting ``close()`` here,
    because this synchronous fixture has no running loop; abandoned pools
    from prior test loops are garbage-collected with their loop.
    """
    import app.agent.checkpointer as cp_mod

    cp_mod._pool = None  # noqa: SLF001
    yield
