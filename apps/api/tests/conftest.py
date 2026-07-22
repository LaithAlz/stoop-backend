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
    # AC + 32 hex chars -- matches the real Twilio Account SID shape so the
    # production-only format gate (app/config.py, safety review finding 7)
    # doesn't reject it when a test explicitly constructs environment=
    # "production" Settings for an UNRELATED assertion.
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "ANTHROPIC_API_KEY": "test-anthropic-api-key",
}

for _key, _value in _PLACEHOLDER_ENV.items():
    os.environ.setdefault(_key, _value)

# ---------------------------------------------------------------------------
# Known bleed class (#212 item 1, comment-only note -- no fixture fixes this):
# "dirty-DB bleed" across sender_tick integration tests.
#
# app/agent/draft_sender.py's sender_tick() candidate SELECT is
# INTENTIONALLY unscoped (the production ticker drains every landlord's
# due drafts cluster-wide) -- there is no landlord_id filter to add. Any
# integration test suite run that is killed/interrupted mid-test (Ctrl-C,
# OOM, a crashed worker) can leave an 'approved', already-due draft behind
# in the shared local Postgres database, because these suites clean up
# their own seeded rows in a `finally:` block that never runs on a hard
# kill. The NEXT time any sender_tick test runs against that same dirty
# database, it can claim/send that stray leftover row too, inflating
# whatever a test asserts about the tick's raw/global counts (observed
# empirically as `claimed == 2` where a clean run would see `1`).
#
# Full per-worker DB isolation (or transaction-rollback-based test
# fixtures instead of manual `DELETE ... WHERE landlord_id = :lid` cleanup)
# would close this class properly but is out of scope for a test-only
# change. The pragmatic fix applied in tests/test_agent_auto_send.py and
# tests/test_agent_draft_sender.py: scope every sender_tick assertion to
# THIS test's own seeded landlord/case/draft/tenant-phone (never a raw
# global `claimed == N` or unscoped `sender.calls` count) -- a stray row
# can add noise elsewhere but can no longer flip an otherwise-correct
# test's result. Residual: a small number of tests (the deadline-budget
# test and the stuck-'sending'-on-provider-failure test) still rely on
# WHICH due draft a shared tick processes FIRST (a single-shot fake-clock
# advance / a single-shot simulated provider failure) -- an old enough
# stray row could still be selected before the test's own row and change
# that specific test's premise, not just its count. If you hit a stray
# flake in one of those two, `TRUNCATE`-cleaning the local dev database
# (see the debugging playbook) is the real fix, not a bigger assertion.
# ---------------------------------------------------------------------------


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
def _reset_scheduler() -> Iterator[None]:
    """Forget the scheduler's module-global ticker task reference between
    tests — same cross-loop hazard as ``_reset_checkpointer_pool`` below:
    an ``asyncio.Task`` binds to the event loop that created it, and each
    test runs its own loop (``asyncio_default_fixture_loop_scope=function``).
    A task surviving across tests would be a latent cross-loop flake the
    moment any later test awaited/cancelled it via ``stop_scheduler()``.
    """
    import app.scheduler as scheduler_mod

    scheduler_mod.reset_scheduler_for_tests()
    yield


@pytest.fixture(autouse=True)
def _reset_twilio_sender() -> Iterator[None]:
    """Drop any injected fake Twilio sender between tests — same
    cross-test-leakage rationale as the JWKS/weather/checkpointer resets
    above: a fake sender set by one test must never leak into another
    test that expects the lazy-construction default (or a DIFFERENT fake)."""
    import app.integrations.twilio_send as twilio_send_mod

    twilio_send_mod.set_twilio_sender_for_tests(None)
    yield


@pytest.fixture(autouse=True)
def _reset_twilio_provisioner() -> Iterator[None]:
    """Drop any injected fake Twilio provisioner between tests (#53) —
    same cross-test-leakage rationale as ``_reset_twilio_sender`` above."""
    import app.integrations.twilio_provision as twilio_provision_mod

    twilio_provision_mod.set_twilio_provisioner_for_tests(None)
    yield


@pytest.fixture(autouse=True)
def _reset_ack_rate_limiter() -> Iterator[None]:
    """Clear the ack-surface rate limiter's in-memory state between tests
    — same cross-test-leakage rationale as the other resets in this file:
    a token hammered by one test must not leave a later test (which may
    reuse a similar/short token in a tight loop) pre-throttled."""
    import app.routers.notifications as notifications_mod

    notifications_mod.reset_rate_limiter_for_tests()
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
