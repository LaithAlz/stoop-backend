"""Tests for app/agent/case_lock_pool.py — the dedicated advisory-lock
connection pool (#186 item 1, "pool starvation under burst").

These tests PIN the shape of the fix, not just its existence: a future
change that quietly re-routes ``app.agent.graph._case_lock`` back onto the
shared admin pool (defeating the whole point of this module) must fail
these tests red, not silently regress into the deadlock the #186 issue
describes (ten concurrent, distinct-case ``run_graph`` calls exhausting a
SHARED pool between long-held lock connections and briefly-held node
checkouts).

The genuine per-case serialization proofs (the barrier-forced concurrent
tests proving ``pg_advisory_xact_lock`` itself still correctly serializes
two callers for the SAME case) already live in
``tests/test_agent_shadow_interrupt.py``'s "Concurrency" section and are
deliberately left untouched by this issue — this module only pins that the
lock now draws its connection from a genuinely SEPARATE pool, and that
pool's own configuration.
"""

from __future__ import annotations

import inspect
import re
import uuid
from typing import Any

import pytest
from sqlalchemy.pool import AsyncAdaptedQueuePool

import app.agent.case_lock_pool as case_lock_pool_mod
import app.agent.graph as graph_mod
import app.db.session as db_session_mod

_UUID_NAME_RE = re.compile(
    r"^__asyncpg_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}__$"
)


def _connect_args_actually_wired_into_engine(engine: Any) -> dict[str, Any]:
    """Duplicated from ``tests/test_db_engine.py`` (self-contained test
    module convention, per this repo's established precedent) — see that
    module's own docstring for the full rationale: reading the connect
    -args back OFF the constructed engine (rather than only asserting on
    the ``_ASYNCPG_POOLER_CONNECT_ARGS`` constant's object identity) proves
    the dict is actually wired INTO the engine ``app.agent.case_lock_pool``
    constructs, not merely defined-but-unused (safety review, #186
    follow-up round, ADVISORY-4(c))."""
    creator = engine.sync_engine.pool._creator  # noqa: SLF001
    cells = dict(zip(creator.__code__.co_freevars, creator.__closure__, strict=True))
    cparams: dict[str, Any] = dict(cells["cparams"].cell_contents)
    return cparams


@pytest.mark.unit
def test_case_lock_engine_is_a_distinct_object_from_the_admin_engine() -> None:
    """The dedicated case-lock engine must never be (or become, via some
    future refactor) the SAME object as ``app.db.session.engine`` — that
    would silently re-merge the two pools #186 item 1 exists to separate."""
    assert case_lock_pool_mod.case_lock_engine is not db_session_mod.engine
    assert case_lock_pool_mod.case_lock_engine is not db_session_mod.request_engine


@pytest.mark.unit
def test_case_lock_engine_pool_object_is_distinct_from_the_admin_pool_object() -> None:
    """Not just a different ``AsyncEngine`` wrapper — the underlying
    connection ``Pool`` object itself must be a separate pool of physical
    connections, never merely a second handle onto the admin engine's own
    pool."""
    assert case_lock_pool_mod.case_lock_engine.pool is not db_session_mod.engine.pool


@pytest.mark.unit
def test_case_lock_pool_size_and_overflow_pin_the_reduced_idle_footprint() -> None:
    """Pinned per the module's own sizing rationale (#186 follow-up round,
    ADVISORY-1): the dedicated pool's PEAK capacity (``pool_size +
    max_overflow``) stays in the SAME 10-connection class the admin engine
    established, but the STEADY-STATE floor (``pool_size``) is smaller —
    this pool's connections are the longest-lived/most
    idle-in-transaction-prone of the four per-process pools (module
    docstring "Full per-process connection-budget accounting"), so fewer
    of them sit open by default."""
    pool = case_lock_pool_mod.case_lock_engine.pool
    admin_pool = db_session_mod.engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    assert pool.size() == case_lock_pool_mod._LOCK_POOL_SIZE  # noqa: SLF001
    assert pool._max_overflow == case_lock_pool_mod._LOCK_MAX_OVERFLOW  # noqa: SLF001

    # Smaller floor than the admin engine's own pool_size=5 -- the whole
    # point of ADVISORY-1.
    assert pool.size() < admin_pool.size()
    # Same PEAK ceiling as before (and as the admin engine) -- ADVISORY-1
    # narrows the idle footprint, it does not change how many concurrent
    # case-locks this process can hold at once (the "Bounded try-lock
    # retries" fix in app/agent/graph.py assumes this peak is unchanged).
    peak = pool.size() + pool._max_overflow  # noqa: SLF001
    admin_peak = admin_pool.size() + admin_pool._max_overflow  # noqa: SLF001
    assert peak == admin_peak == 10


@pytest.mark.unit
def test_case_lock_pool_timeout_is_shorter_than_the_admin_pools_default() -> None:
    """The dedicated pool must fail a checkout FASTER than the admin engine
    would — the #186 issue's prescribed safe-failure direction (a timed-out
    checkout propagates to the existing Sentry/needs_eyes fallback in
    ``app/agent/graph_entry.py`` rather than blocking a tenant-facing
    request for the admin engine's full 30s default)."""
    lock_timeout = case_lock_pool_mod.case_lock_engine.pool._timeout  # noqa: SLF001
    admin_timeout = db_session_mod.engine.pool._timeout  # noqa: SLF001
    assert lock_timeout == case_lock_pool_mod._LOCK_POOL_TIMEOUT  # noqa: SLF001
    assert lock_timeout < admin_timeout


@pytest.mark.unit
def test_case_lock_pool_module_imports_the_admin_engines_own_pooler_connect_args_object() -> None:
    """Must reuse the EXACT SAME ``_ASYNCPG_POOLER_CONNECT_ARGS`` object
    ``app/db/session.py`` exports (not a re-implemented, driftable copy) —
    the three Supavisor/PgBouncer compatibility knobs must never silently
    diverge between this pool and the admin/request engines. Object
    -identity only (import wiring); :func:`test_case_lock_engine_connect_
    args_are_actually_wired_into_the_engine` below proves it is ALSO
    genuinely applied to the constructed engine, not merely imported and
    unused."""
    assert (
        case_lock_pool_mod._ASYNCPG_POOLER_CONNECT_ARGS  # noqa: SLF001
        is db_session_mod._ASYNCPG_POOLER_CONNECT_ARGS  # noqa: SLF001
    )


@pytest.mark.unit
def test_case_lock_engine_connect_args_are_actually_wired_into_the_engine() -> None:
    """Safety review, #186 follow-up round, ADVISORY-4(c): the previous
    version of this test only asserted object identity on the imported
    ``_ASYNCPG_POOLER_CONNECT_ARGS`` constant, which does not prove those
    knobs were actually threaded into ``create_async_engine(...)`` — the
    SAME ``_connect_args_actually_wired_into_engine`` technique
    ``tests/test_db_engine.py`` uses for the admin engine, applied here to
    ``case_lock_engine``, closes that gap: it reads the connect-args back
    OFF the constructed engine's own pool ``_creator`` closure."""
    cparams = _connect_args_actually_wired_into_engine(case_lock_pool_mod.case_lock_engine)

    assert cparams.get("prepared_statement_cache_size") == 0
    assert cparams.get("statement_cache_size") == 0
    name_func = cparams.get("prepared_statement_name_func")
    assert callable(name_func)

    first = name_func()
    second = name_func()
    assert first != second, "two successive calls produced the same statement name"
    assert _UUID_NAME_RE.match(first), f"unexpected statement name format: {first!r}"
    assert _UUID_NAME_RE.match(second), f"unexpected statement name format: {second!r}"


@pytest.mark.unit
def test_case_lock_function_checks_out_from_the_dedicated_pool_not_get_admin_session() -> None:
    """Source-level pin (mirrors ``tests/test_db_engine.py``'s own
    ``inspect.getsource`` convention for wiring checks): ``_case_lock``'s
    body must call ``get_case_lock_session``, never
    ``get_admin_session`` — a future edit that reverts the lock back onto
    the shared admin pool must fail this test, not just silently regress
    throughput under burst."""
    source = inspect.getsource(graph_mod._case_lock)  # noqa: SLF001
    assert "get_case_lock_session" in source
    assert "get_admin_session" not in source


@pytest.mark.unit
def test_case_lock_pool_module_exports_only_the_documented_public_surface() -> None:
    """Pins the module's intended public API — a future addition of a
    second, differently-configured engine/pool here (re-fragmenting the
    lock's connection source) should be a deliberate, reviewed change, not
    a silent one this test lets slip through unnoticed."""
    assert set(case_lock_pool_mod.__all__) == {
        "CaseLockSessionFactory",
        "case_lock_engine",
        "get_case_lock_session",
    }


# ---------------------------------------------------------------------------
# Genuine proof against a real Postgres instance — not just object-identity
# pins above. Marker: integration (requires docker-compose Postgres).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_holding_the_case_lock_adds_zero_to_the_admin_pools_checked_out_count() -> None:
    """The actual #186 item 1 regression proof: while ``_case_lock`` holds
    its (long-held, LLM-span-duration in production) connection, the ADMIN
    pool -- the one every in-span node checks out its own, separate,
    briefly-held connection from -- must show NO ADDITIONAL connections
    checked out on the lock's behalf. Before this fix, ``_case_lock``
    checked out an ADMIN-pool connection for this exact span; this
    assertion would have failed then (the admin pool's checked-out DELTA
    would include the lock's own connection) and must stay passing now
    that the two pools are genuinely separate.

    Safety review, #186 follow-up round, ADVISORY-4(d): asserts a DELTA
    against a snapshot taken immediately before entering the lock, rather
    than asserting the raw count is exactly zero -- a prior test in the
    same run (or a genuinely concurrent one) can leave the SHARED,
    process-wide admin pool with an unrelated connection already checked
    out (e.g. one still winding down its own commit/close), which an
    assert-zero form would misreport as this test's own failure. The delta
    form is robust to that: it only ever fails if ENTERING ``_case_lock``
    itself increased the admin pool's checked-out count, which is the
    actual regression this test exists to catch.
    """
    case_id = uuid.uuid4()
    admin_checked_out_before = db_session_mod.engine.pool.checkedout()
    async with graph_mod._case_lock(case_id):  # noqa: SLF001
        assert db_session_mod.engine.pool.checkedout() == admin_checked_out_before
        assert case_lock_pool_mod.case_lock_engine.pool.checkedout() >= 1
