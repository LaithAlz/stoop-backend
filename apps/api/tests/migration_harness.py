"""Shared harness for the ``downgrade base`` -> ``upgrade head`` migration
test fixture (issue #281).

Each ``tests/test_migrations_*.py`` module (plus a handful of other
integration-test modules that also need a clean, fully-migrated database:
``test_rls_isolation.py``, ``test_rls_isolation_matrix.py``,
``test_checkpointer.py``, ``test_require_landlord.py``) deliberately
duplicates its OWN ``_get_db_url``/``_alembic`` subprocess helpers — an
established convention in this repo, so each integration-test module stays
runnable and debuggable on its own (see e.g. ``test_migrations_0007.py``'s
"Helpers — duplicated (not imported)" comment). This module does NOT
change that: ``_get_db_url``/``_alembic`` stay local to each file.

What WAS duplicated across ~19 of those files' own session-scoped
``_migrate_once`` fixtures, byte-for-byte, is the two-line body
``_alembic("downgrade", "base"); _alembic("upgrade", "head")``. That
duplication is what this module removes — not for DRY's own sake, but
because migration 0009's ``downgrade()`` intentionally FAILS CLOSED if a
``tenant_ack``/``degraded_retry`` row still exists in ``notifications``
(see ``migrations/versions/0009_degraded_mode_notification_types.py``'s
module docstring, "ROUND-TRIP" section): a full test-suite run killed
mid-flight can leave exactly such a row behind, and every one of these
~19 downgrade-base fixtures then fails — deterministically, not flakily —
the next time anyone runs them against that same, now-dirty, lane
database. Without this module, that surfaces as a cascade of ~200
confusing setup errors with nothing pointing at the actual cause.

``migrate_from_base_to_head()`` centralizes catching THAT one specific
failure shape (see its own docstring) so the fix — and any future
adjustment to it — lives in one place instead of copy-pasted 19 times.
"""

from __future__ import annotations

from collections.abc import Callable

# Markers that, ALL THREE together, uniquely identify migration 0009's own
# fail-closed guard having fired during a "downgrade base" run:
#   1. alembic's own progress log names the exact step ("Running downgrade
#      0009 -> 0008") that was executing when the subprocess died.
#   2. the underlying Postgres error class is specifically a check-
#      violation (not a connection error, a syntax error, a missing-object
#      error, ...).
#   3. the specific constraint is notifications_type_check.
# Requiring all three (rather than e.g. just #2+#3) rules out a
# structurally-identical but genuinely different failure — migration
# 0011's downgrade narrows the SAME notifications_type_check constraint
# for its own 'number_release' type and can fail closed the same way; its
# step marker would read "Running downgrade 0011 -> 0010", not this one,
# so it correctly does NOT match here (see LaneDatabaseNeedsRecreateError's
# docstring — that is a known, separate instance of the same mechanism,
# out of scope for #281, which names 0009 specifically).
_DOWNGRADE_0009_STEP_MARKER = "Running downgrade 0009 -> 0008"
_CHECK_VIOLATION_MARKER = "CheckViolationError"
_NOTIFICATIONS_TYPE_CHECK_MARKER = 'check constraint "notifications_type_check"'


class LaneDatabaseNeedsRecreateError(RuntimeError):
    """Raised in place of the raw alembic-subprocess ``RuntimeError`` when
    ``downgrade base`` dies specifically inside migration 0009's
    fail-closed guard — i.e. leftover ``tenant_ack``/``degraded_retry``
    rows in ``notifications``, from an interrupted prior test run, that
    the migration correctly refuses to silently discard.

    Deliberately NOT raised for:

    - any OTHER alembic failure (a genuine migration bug, a DB connection
      error, an unrelated constraint) — those propagate as the original
      ``RuntimeError``, unchanged;
    - a DELIBERATE trip of this same guard inside a test body (e.g.
      ``test_migrations_0009.py::
      test_downgrade_fails_closed_when_tenant_ack_row_exists``, which
      downgrades to a specific revision, not ``"base"``, calling
      ``_alembic()`` directly rather than going through
      ``migrate_from_base_to_head()`` — that test's own
      ``pytest.raises(RuntimeError, match="notifications_type_check")``
      is intentionally left untouched by this module);
    - migration 0011's structurally-identical ``number_release`` guard on
      the same ``notifications_type_check`` constraint (a different
      downgrade step; not detected here — see the module docstring).
    """


def migrate_from_base_to_head(alembic_runner: Callable[..., None]) -> None:
    """The shared ``_migrate_once`` fixture body: ``downgrade base`` then
    ``upgrade head``, run via the CALLER's own local ``_alembic`` helper
    (dependency-injected, not imported — each test module keeps its own
    subprocess/DB-url helpers per this repo's established
    self-contained-integration-test convention; only the sequencing +
    failure-translation below is shared).

    If ``downgrade base`` fails for exactly one reason — migration 0009's
    fail-closed guard firing because a ``tenant_ack``/``degraded_retry``
    row from an interrupted PRIOR run is still sitting in
    ``notifications`` — re-raise a ``LaneDatabaseNeedsRecreateError``
    naming the cause and the remedy, chained (``raise ... from exc``) onto
    the original ``RuntimeError`` so the raw alembic/Postgres output is
    still visible to anyone who wants it. Any OTHER failure (a genuine
    migration bug, a connection error, ...) propagates completely
    unchanged — this must never become a catch-all.
    """
    try:
        alembic_runner("downgrade", "base")
    except RuntimeError as exc:
        failure_text = str(exc)
        if (
            _DOWNGRADE_0009_STEP_MARKER in failure_text
            and _CHECK_VIOLATION_MARKER in failure_text
            and _NOTIFICATIONS_TYPE_CHECK_MARKER in failure_text
        ):
            raise LaneDatabaseNeedsRecreateError(
                "downgrade base stopped at migration 0009: notifications "
                "still has one or more leftover tenant_ack/degraded_retry "
                "row(s), most likely left behind by a full test-suite run "
                "that was killed mid-flight against this lane database. "
                "Migration 0009's downgrade() intentionally fails closed "
                "rather than silently discard rows it cannot reconstruct "
                "(see that migration's module docstring, 'ROUND-TRIP' "
                "section) — this is not a test failure or a migration bug. "
                "Remedy: recreate this lane database (drop it, create it "
                "fresh, then `alembic upgrade head`) and rerun."
            ) from exc
        raise
    alembic_runner("upgrade", "head")
