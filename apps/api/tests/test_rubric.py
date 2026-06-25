"""Rubric drift-guard tests.

These are the load-bearing checksum tests that enforce rule #4:
"The rubric is embedded verbatim — a checksum test enforces it."

If the doc changes without updating rubric.py, or rubric.py drifts from
the doc, ONE of these assertions fails:
  1. The byte-for-byte equality check catches content drift.
  2. The pinned sha256 catches drift in either the doc OR rubric.py even
     if someone updates both but introduces a subtle difference.

Pinned sha256 (must be updated together with rubric.py AND the doc, with
a full eval run — see apps/api/CLAUDE.md):
  469cd67017c3b5604550c488bb1a6840ebeabb31a55e3a9b5a46a47014d18484
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app.agent.rubric import RUBRIC_V1, RUBRIC_VERSION

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the source-of-truth doc.  Anchored from this test file so it works
# regardless of where pytest is invoked from.
_DOC_PATH: Path = Path(__file__).parents[3] / "docs" / "02-product" / "severity-rubric-v1.md"

# Pinned checksum.  Changing this constant without a new rubric version file
# and a full eval run violates the project convention.
_PINNED_SHA256: str = "469cd67017c3b5604550c488bb1a6840ebeabb31a55e3a9b5a46a47014d18484"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_rubric_from_doc(doc_path: Path) -> str:
    """Extract the verbatim rubric text from the ```text fence in the doc.

    Strategy:
      1. Find the "## The rubric (verbatim, v1.0)" heading.
      2. Find the FIRST ```text fence after that heading.
      3. Collect lines until the closing ```.
      4. Join with newlines (matching how the raw file lines are stored).
      5. Strip a single trailing newline consistently so the comparison is
         not defeated by a lone trailing \\n added by editors.
    """
    content: str = doc_path.read_text(encoding="utf-8")
    lines: list[str] = content.split("\n")

    heading_pattern: re.Pattern[str] = re.compile(r"^##\s+The rubric \(verbatim")
    fence_open: re.Pattern[str] = re.compile(r"^```text\s*$")
    fence_close: re.Pattern[str] = re.compile(r"^```\s*$")

    state: str = "before_heading"
    rubric_lines: list[str] = []

    for line in lines:
        if state == "before_heading":
            if heading_pattern.match(line):
                state = "after_heading"
        elif state == "after_heading":
            if fence_open.match(line):
                state = "in_fence"
        elif state == "in_fence":
            if fence_close.match(line):
                break
            rubric_lines.append(line)

    assert rubric_lines, (
        f"Could not extract rubric text block from {doc_path}. "
        "Check that the heading and ```text fence are present."
    )

    return "\n".join(rubric_lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rubric_version_constant() -> None:
    """RUBRIC_VERSION must be the string '1.0'."""
    assert RUBRIC_VERSION == "1.0"


@pytest.mark.unit
def test_rubric_doc_exists() -> None:
    """The severity rubric doc must be present in the repo."""
    assert _DOC_PATH.is_file(), (
        f"Expected rubric doc at {_DOC_PATH}. "
        "If the file was moved, update _DOC_PATH and open a spec PR."
    )


@pytest.mark.unit
def test_rubric_matches_doc_verbatim() -> None:
    """RUBRIC_V1 must be byte-identical to the ```text block in the doc.

    This is the primary drift guard: if the doc is edited without updating
    rubric.py (or vice-versa), this test fails immediately.
    """
    doc_rubric: str = _extract_rubric_from_doc(_DOC_PATH)

    # Strip a single trailing newline on both sides so an editor that appends
    # a final \\n to the fence block doesn't cause a spurious failure.
    assert RUBRIC_V1.rstrip("\n") == doc_rubric.rstrip("\n"), (
        "RUBRIC_V1 in app/agent/rubric.py does not match the verbatim "
        "block in docs/02-product/severity-rubric-v1.md.\n"
        "To fix: copy the ```text block from the doc into rubric.py exactly.\n"
        "To change the rubric: create a new version file — never edit in place."
    )


@pytest.mark.unit
def test_rubric_pinned_sha256() -> None:
    """RUBRIC_V1 must hash to the pinned sha256.

    This catches drift in EITHER the doc OR rubric.py even when both are
    updated but a subtle character difference is introduced.  Updating this
    constant requires a new rubric version file + full eval run.
    """
    actual_sha: str = hashlib.sha256(RUBRIC_V1.encode("utf-8")).hexdigest()
    assert actual_sha == _PINNED_SHA256, (
        f"RUBRIC_V1 sha256 mismatch.\n"
        f"  expected : {_PINNED_SHA256}\n"
        f"  actual   : {actual_sha}\n"
        "To update the pinned hash you must also create a new rubric version "
        "file and run the full eval suite (uv run pytest -m eval)."
    )


@pytest.mark.unit
def test_rubric_starts_with_expected_header() -> None:
    """Sanity-check: RUBRIC_V1 starts with the expected header line."""
    assert RUBRIC_V1.startswith("SEVERITY RUBRIC v1.0 — Stoop"), (
        "RUBRIC_V1 does not start with the expected header. "
        "Check for leading whitespace or encoding issues."
    )


@pytest.mark.unit
def test_rubric_ends_with_expected_footer() -> None:
    """Sanity-check: RUBRIC_V1 ends with the expected footer line."""
    expected_end: str = (
        "Severity, the rule(s) above that fired, the modifier if applied, refusal\n"
        "flags if any, and one-sentence reasoning per issue found."
    )
    assert RUBRIC_V1.endswith(expected_end), (
        "RUBRIC_V1 does not end with the expected footer. "
        "Check for trailing whitespace or line-ending issues."
    )
