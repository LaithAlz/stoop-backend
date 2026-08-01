"""Pin coverage for the ``langgraph`` ceiling version constraint (#186 item 4).

``app/agent/graph.py``'s "Stale-draft re-run" docstring documents an
empirically-verified (not officially documented/guaranteed) behavior of
langgraph 1.2.7: a plain ``ainvoke(new_state, config)`` on a thread currently
paused at a live ``interrupt()`` restarts the run from ``START`` using the
new input, discarding the pending task, rather than silently replaying the
stale one. The whole stale-draft supersession design (and the correction of
an earlier revision's flawed probe, same docstring's "Correction" section)
rests on that exact semantics. ``pyproject.toml`` pins
``langgraph>=1.2.7,<1.3`` so a silent minor-version bump can never reopen
that silent-staleness failure mode without a deliberate, reviewed
re-verification (a new ceiling bump + re-running
``tests/test_agent_shadow_interrupt.py``, the regression guard the pinned
semantics rely on).

These are config-level pins, not behavioural tests (no I/O) — same
convention as ``tests/test_db_engine.py``'s
``test_pooler_connect_args_constant_pinned``: they exist so a future
"cleanup" that widens or drops the ceiling fails red immediately, instead of
silently shipping an unverified langgraph upgrade.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _langgraph_requirement() -> Requirement:
    data = tomllib.loads(_PYPROJECT_PATH.read_text())
    dependencies: list[str] = data["project"]["dependencies"]
    langgraph_specs = [dep for dep in dependencies if Requirement(dep).name == "langgraph"]
    assert len(langgraph_specs) == 1, (
        f"expected exactly one `langgraph` dependency entry, found {langgraph_specs!r}"
    )
    return Requirement(langgraph_specs[0])


@pytest.mark.unit
def test_langgraph_dependency_has_a_ceiling_pin() -> None:
    """``pyproject.toml``'s ``langgraph`` dependency must include an upper
    bound (``<1.3``) — a floor-only constraint (``>=1.2.7``) would let a
    silent minor bump reopen the silent-staleness mode this ceiling exists
    to prevent."""
    requirement = _langgraph_requirement()

    assert Version("1.2.7") in requirement.specifier
    assert Version("1.3.0") not in requirement.specifier, (
        "the langgraph ceiling pin must exclude 1.3.0 and later -- widen this "
        "constraint only after re-verifying the ainvoke-restarts-from-START "
        "semantics (app/agent/graph.py's 'Stale-draft re-run' docstring) "
        "against the new version and re-running "
        "tests/test_agent_shadow_interrupt.py"
    )


@pytest.mark.unit
def test_installed_langgraph_satisfies_the_ceiling_pin() -> None:
    """The currently-installed ``langgraph`` version must satisfy
    ``pyproject.toml``'s own pin -- confirms the pin is not just present but
    consistent with what this environment actually runs against."""
    requirement = _langgraph_requirement()
    installed = Version(version("langgraph"))

    assert installed in requirement.specifier, (
        f"installed langgraph {installed} does not satisfy the pin "
        f"{requirement.specifier} -- pyproject.toml and uv.lock have drifted "
        "apart from what is actually installed"
    )
    # The specific version the "Stale-draft re-run" empirical verification
    # was run against (module docstring in app/agent/graph.py) -- a floor
    # check, not an equality pin: any patch release on the 1.2.x line is
    # expected to preserve this semantics, only a minor bump is gated by
    # the ceiling above.
    assert installed >= Version("1.2.7")
