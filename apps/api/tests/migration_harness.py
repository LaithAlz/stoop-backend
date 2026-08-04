"""Shared harness for the ``downgrade base`` -> ``upgrade head`` migration
test fixture (issue #281).

Each ``tests/test_migrations_*.py`` module (plus a handful of other
integration-test modules that also need a clean, fully-migrated database:
``test_rls_isolation.py``, ``test_rls_isolation_matrix.py``,
``test_checkpointer.py``, ``test_require_landlord.py``) deliberately
duplicates its OWN ``_get_db_url``/``_alembic`` subprocess helpers, an
established convention in this repo, so each integration-test module stays
runnable and debuggable on its own (see e.g. ``test_migrations_0007.py``'s
"Helpers, duplicated (not imported)" comment). This module does NOT change
that: ``_get_db_url``/``_alembic`` stay local to each file.

What WAS duplicated across ~19 of those files' own session-scoped
``_migrate_once`` fixtures, byte-for-byte, is the two-line body
``_alembic("downgrade", "base"); _alembic("upgrade", "head")``. That
duplication is what this module removes, not for DRY's own sake, but
because more than one migration's ``downgrade()`` intentionally FAILS
CLOSED when it would otherwise have to silently discard live rows it
cannot reconstruct (see ``NOTIFICATIONS_TYPE_CHECK_GUARDS`` below): a full
test-suite run killed mid-flight can leave exactly such a row behind, and
every one of these ~19 downgrade-base fixtures then fails, deterministically,
not flakily, the next time anyone runs them against that same, now-dirty,
lane database. Without this module, that surfaces as a cascade of ~200
confusing setup errors with nothing pointing at the actual cause.

``migrate_from_base_to_head()`` centralizes catching THAT failure shape
(see its own docstring) so the fix, and any future adjustment to it, lives
in one place instead of copy-pasted 19 times.

DETECTION STRATEGY: probe, don't parse logs
--------------------------------------------
An earlier version of this module distinguished "one of these known
guards fired" from "some other migration failure happened" by pattern
-matching alembic's own progress-log text (e.g. the literal line
``Running downgrade 0009 -> 0008``). That coupled a diagnostic to a log
format this repo does not own: if a future alembic version changes how
(or whether) it logs that line, the match silently stops firing, the raw
cascade comes back, and nothing tells anyone the diagnostic broke, which
is worse than never having it (safety review, #281 follow-up).

This version instead probes the database directly after ANY
``downgrade base`` failure: it counts live ``notifications`` rows whose
``type`` is one of the values any KNOWN guard protects
(``NOTIFICATIONS_TYPE_CHECK_GUARDS`` below). A non-zero count IS the
diagnosis, no log text involved, and the count is real, useful
information the old text-matching approach could never have provided.
A zero count means whatever actually failed is unrelated to any known
guard, and the original error propagates completely unchanged, no
guessing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@dataclass(frozen=True)
class NotificationsTypeCheckGuard:
    """One fail-closed guard on the ``notifications_type_check`` CHECK
    constraint: a migration revision whose ``downgrade()`` re-narrows that
    constraint and refuses to run (raises, rolls back, stays put) while
    live rows of its own guarded ``notifications.type`` value(s) still
    exist. See that revision's own module docstring, "ROUND-TRIP" section,
    for the individual hazard analysis.

    Adding a newly-discovered guard here is the entire change needed to
    cover it: no other code in this module branches on the revision.
    """

    revision: str
    guarded_types: tuple[str, ...]


NOTIFICATIONS_TYPE_CHECK_GUARDS: tuple[NotificationsTypeCheckGuard, ...] = (
    NotificationsTypeCheckGuard(
        revision="0009",
        guarded_types=("tenant_ack", "degraded_retry"),
    ),
    NotificationsTypeCheckGuard(
        revision="0011",
        guarded_types=("number_release",),
    ),
)

_ALL_GUARDED_TYPES: tuple[str, ...] = tuple(
    guarded_type
    for guard in NOTIFICATIONS_TYPE_CHECK_GUARDS
    for guarded_type in guard.guarded_types
)


class LaneDatabaseNeedsRecreateError(RuntimeError):
    """Raised in place of the raw alembic-subprocess ``RuntimeError`` when
    ``downgrade base`` fails AND the database, probed directly right
    afterward, still has one or more live rows whose ``notifications.type``
    is guarded by ``NOTIFICATIONS_TYPE_CHECK_GUARDS`` (e.g. leftover
    ``tenant_ack``/``degraded_retry`` rows from migration 0009's guard, or
    a leftover ``number_release`` row from migration 0011's), most likely
    left behind by an interrupted prior test run.

    Deliberately NOT raised when the probe finds zero such rows: any OTHER
    alembic failure (a genuine migration bug, a DB connection error, an
    unrelated constraint on a different table entirely) propagates as the
    original ``RuntimeError``, unchanged.

    Also deliberately NOT raised for a DELIBERATE trip of one of these
    guards inside a test body, e.g. ``test_migrations_0009.py::
    test_downgrade_fails_closed_when_tenant_ack_row_exists``, which
    downgrades to a specific revision, not ``"base"``, calling the local
    ``_alembic()`` helper directly rather than going through
    ``migrate_from_base_to_head()``; that test's own
    ``pytest.raises(RuntimeError, match="notifications_type_check")`` is
    intentionally left untouched by this module.
    """


async def _count_leftover_guarded_rows(db_url: str) -> dict[str, int]:
    """Count live ``notifications`` rows per guarded ``type``.

    Returns only ``{type_name: count}`` for types with at least one row.
    Never reads or returns ``payload``, ``landlord_id``, or any other
    column: counts and type names are the only thing any caller can see
    (never-break rule 5, no row content, no phone numbers, no message
    bodies, no tokens).
    """
    engine = create_async_engine(db_url, echo=False)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT type, count(*) AS n FROM notifications "
                    "WHERE type = ANY(:types) GROUP BY type"
                ),
                {"types": list(_ALL_GUARDED_TYPES)},
            )
            rows = result.mappings().all()
    finally:
        await engine.dispose()
    return {row["type"]: row["n"] for row in rows if row["n"]}


def _describe_leftover_rows(leftover: dict[str, int]) -> str:
    """Render ``{type: count}`` grouped by the guard (and its revision)
    that protects each type, e.g. ``migration 0009 (tenant_ack=1)``. Only
    counts and type/revision names, never row content."""
    parts: list[str] = []
    for guard in NOTIFICATIONS_TYPE_CHECK_GUARDS:
        guard_counts = {t: leftover[t] for t in guard.guarded_types if t in leftover}
        if guard_counts:
            counts_str = ", ".join(f"{t}={n}" for t, n in guard_counts.items())
            parts.append(f"migration {guard.revision} ({counts_str})")
    return "; ".join(parts)


def migrate_from_base_to_head(alembic_runner: Callable[..., None], db_url: str) -> None:
    """The shared ``_migrate_once`` fixture body: ``downgrade base`` then
    ``upgrade head``, run via the CALLER's own local ``_alembic`` helper
    (dependency-injected, not imported, each test module keeps its own
    subprocess/DB-url helpers per this repo's established
    self-contained-integration-test convention; only the sequencing +
    failure-translation below is shared). ``db_url`` is the same value the
    caller's own ``_get_db_url()`` would return, used ONLY to open a
    diagnostic probe connection if ``downgrade base`` fails.

    If ``downgrade base`` fails, probe the database for leftover rows of
    any known guarded ``notifications.type`` (see module docstring,
    "DETECTION STRATEGY"). If any are found, that IS the diagnosis:
    re-raise a ``LaneDatabaseNeedsRecreateError`` naming the cause, the
    real counts, and the remedy, chained (``raise ... from exc``) onto the
    original ``RuntimeError`` so the raw alembic/Postgres output is still
    visible to anyone who wants it. If the probe itself fails, or finds
    nothing, the original failure propagates completely unchanged, this
    must never become a catch-all, and a broken diagnostic must never hide
    the real error it was trying to explain.
    """
    try:
        alembic_runner("downgrade", "base")
    except RuntimeError as exc:
        try:
            # Deliberately broad: the probe is a best-effort diagnostic, not
            # the real error. If it fails for ANY reason (bad db_url, no
            # connection, an unexpected schema), that must never replace or
            # hide the original alembic failure below; fall through to it.
            leftover = asyncio.run(_count_leftover_guarded_rows(db_url))
        except Exception:
            leftover = {}
        if leftover:
            raise LaneDatabaseNeedsRecreateError(
                "downgrade base failed, and notifications still has leftover "
                f"row(s) from an interrupted prior test run: {_describe_leftover_rows(leftover)}. "
                "A migration's downgrade() intentionally fails closed rather than "
                "silently discard rows it cannot reconstruct (see the guarding "
                "migration's own module docstring, ROUND-TRIP section). This is "
                "not a test failure or a migration bug. Remedy: recreate this lane "
                "database (drop it, create it fresh, then `alembic upgrade head`) "
                "and rerun."
            ) from exc
        raise
    alembic_runner("upgrade", "head")
