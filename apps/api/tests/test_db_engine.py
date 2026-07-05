"""Pin the Supavisor/PgBouncer transaction-mode pooler compatibility config.

Regression coverage for ``asyncpg.exceptions.DuplicatePreparedStatementError``,
reproduced live against the real Supabase transaction pooler (port 6543):
asyncpg's SQLAlchemy dialect (and asyncpg itself) must have their prepared
statement caches disabled and use a UUID-based statement-name function, or
named prepared statements collide across pooled connections in Supavisor's
transaction mode.

Three ``connect_args`` keys are required (see ``app/db/session.py`` for the
full rationale, confirmed by live testing):
- ``prepared_statement_cache_size=0`` — SQLAlchemy dialect's own cache.
- ``prepared_statement_name_func`` — uuid4-based names for the dialect's
  ``_prepare()`` path.
- ``statement_cache_size=0`` — asyncpg's OWN driver-level cache, used by its
  raw convenience methods (e.g. ``fetchrow``, as called by
  ``pool_pre_ping``'s ping) which bypass the dialect's ``_prepare()``
  entirely and are unaffected by the two keys above.

These are config-level pins, not behavioural DB tests (no I/O) — they exist so
a future "cleanup" that drops any of the ``connect_args`` from
``create_async_engine`` fails red immediately, instead of surfacing only as
an intermittent production error against the pooler. See ``app/db/session.py``
(module docstring + ``_ASYNCPG_POOLER_CONNECT_ARGS``) and ``migrations/env.py``
for the exact SQLAlchemy asyncpg-dialect docstring recipe followed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

import app.db.session as db_mod

_UUID_NAME_RE = re.compile(
    r"^__asyncpg_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}__$"
)


def _connect_args_actually_wired_into_engine(engine: Any) -> dict[str, Any]:
    """Return the DBAPI ``connect()`` kwargs actually captured by the engine.

    ``create_async_engine(..., connect_args={...})`` merges ``connect_args``
    into the connection-parameters dict SQLAlchemy closes over inside its
    internal pool ``_creator`` callable (see
    ``sqlalchemy/engine/create.py``: ``cparams = ... .union(connect_args)``).
    Reading it back here (rather than only asserting on our own
    ``_ASYNCPG_POOLER_CONNECT_ARGS`` constant) proves the dict is actually
    wired into the engine that ``app.db.session`` constructs and exports —
    not merely defined-but-unused.
    """
    creator = engine.sync_engine.pool._creator  # noqa: SLF001
    cells = dict(zip(creator.__code__.co_freevars, creator.__closure__, strict=True))
    cparams: dict[str, Any] = dict(cells["cparams"].cell_contents)
    return cparams


@pytest.mark.unit
def test_app_engine_connect_args_disable_prepared_statement_cache() -> None:
    """The module-level app engine must disable the SQLAlchemy dialect's
    asyncpg statement cache.

    ``prepared_statement_cache_size=0`` forces every ``PREPARE`` issued via
    the dialect's ``_prepare()`` to be a fresh, single-use statement instead
    of one cached (and thus reused across possibly-different pooled
    backends) per DBAPI connection.
    """
    cparams = _connect_args_actually_wired_into_engine(db_mod.engine)
    assert cparams.get("prepared_statement_cache_size") == 0


@pytest.mark.unit
def test_app_engine_connect_args_disable_asyncpg_native_statement_cache() -> None:
    """The app engine must ALSO disable asyncpg's own driver-level cache.

    ``statement_cache_size=0`` (no ``prepared_`` prefix) is forwarded
    straight through to ``asyncpg.connect()``. It is a *separate* cache from
    the SQLAlchemy-dialect one above, used by asyncpg's own raw convenience
    methods (``fetchrow``/``fetch``/``fetchval``/``execute``) — notably the
    one ``pool_pre_ping`` uses to ping a checked-out connection. Live testing
    against the real pooler showed this cache alone (with the dialect cache
    already disabled) still produces
    ``DuplicatePreparedStatementError: ... "__asyncpg_stmt_<n>__" ...``
    (asyncpg's own sequential auto-naming, distinct from our uuid4 names).
    """
    cparams = _connect_args_actually_wired_into_engine(db_mod.engine)
    assert cparams.get("statement_cache_size") == 0


@pytest.mark.unit
def test_app_engine_connect_args_have_unique_name_func() -> None:
    """The app engine's prepared-statement name func must be a callable that
    yields a fresh, globally-unique ``__asyncpg_<uuid4>__`` name every call.
    """
    cparams = _connect_args_actually_wired_into_engine(db_mod.engine)
    name_func = cparams.get("prepared_statement_name_func")
    assert callable(name_func)

    first = name_func()
    second = name_func()

    assert first != second, "two successive calls produced the same statement name"
    assert _UUID_NAME_RE.match(first), f"unexpected statement name format: {first!r}"
    assert _UUID_NAME_RE.match(second), f"unexpected statement name format: {second!r}"


@pytest.mark.unit
def test_pooler_connect_args_constant_pinned() -> None:
    """``_ASYNCPG_POOLER_CONNECT_ARGS`` — the constant reused by the app
    engine — must keep exactly the three pooler-compat keys.

    A future edit that drops any of the three (e.g. assuming the SQLAlchemy
    dialect's ``prepared_statement_cache_size`` alone is sufficient — it is
    not, see the module docstring and ``test_app_engine_connect_args_disable_
    asyncpg_native_statement_cache`` above) must fail this test.
    """
    args = db_mod._ASYNCPG_POOLER_CONNECT_ARGS  # noqa: SLF001

    assert args["prepared_statement_cache_size"] == 0
    assert args["statement_cache_size"] == 0
    assert callable(args["prepared_statement_name_func"])

    name_func: Callable[[], str] = args["prepared_statement_name_func"]
    names = {name_func() for _ in range(10)}
    assert len(names) == 10, "prepared_statement_name_func produced a duplicate name"
    for name in names:
        assert _UUID_NAME_RE.match(name), f"unexpected statement name format: {name!r}"
