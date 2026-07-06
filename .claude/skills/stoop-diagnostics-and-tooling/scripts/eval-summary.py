#!/usr/bin/env python3
"""eval-summary.py -- compact, read-only summary of the Stoop eval gate's
always-written report at apps/api/evals/results/last-run.json.

Usage (from repo root):
    python3 .claude/skills/stoop-diagnostics-and-tooling/scripts/eval-summary.py
    python3 .../eval-summary.py path/to/some-other-run.json   # optional override

Prints:
  1. Run header (generated_at, prompt_version, rubric_version).
  2. One row per scenario:
       scenario | category | prefilter | classification | draft | verdict | cost
     verdict is PASS / HARD-FAIL / SOFT-FAIL / INFRA (INFRA = errored =
     inconclusive, re-run -- never a semantic miss).
  3. The gate verdict line. release_blocked semantics (evals/scoring.py
     GateVerdict.release_blocked): ANY hard failure OR ANY errored
     (inconclusive) scenario blocks release -- "we don't know" is not a
     shippable state.
  4. CHECK-INVERSION warnings: a scenario whose judge BOOLEANS failed while
     its judge_reasoning PROSE contains no negative keywords. That
     disagreement is the fingerprint of the gate-5 judge-verdict-inversion
     bug class (see evals/judge.py, "BLOCKING bug found in gate 5 triage"):
     the judge graded correctly but returned booleans under mismatched keys,
     so the failure is EVAL-INFRA, not product. Heuristic only -- prose that
     happens to contain an incidental negative word suppresses the warning,
     so always eyeball judge_reasoning yourself on any judge failure.
  5. JUDGE-KEY-MISMATCH warnings: any draft failure containing the scorer's
     distinct "NO MATCHING KEY" marker (evals/scoring.py) -- by construction
     an eval-infra output-shape problem, not a content miss.

Read-only. No network. No API calls. Free.
Exit codes: 0 = read OK and release not blocked; 1 = read OK but
release_blocked; 2 = file missing/unreadable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ -> skill dir -> skills/ -> .claude/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PATH = REPO_ROOT / "apps" / "api" / "evals" / "results" / "last-run.json"

# Negative-keyword list for the inversion heuristic. Substring match,
# lowercase. "fail" also catches fails/failed/failure; "violat" catches
# violation/violates/violated; "conformant" issues are caught by
# "non-conformant"/"nonconformant".
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "fail",
    "missing",
    "absent from",
    "not present",
    "not satisfied",
    "not conveyed",
    "not covered",
    "does not",
    "doesn't",
    "isn't",
    "violat",
    "non-conformant",
    "nonconformant",
    "no matching",
    "lacks",
    "lacking",
    "jargon",
    "too long",
    "exceeds",
    "incorrect",
    "wrong",
    "vague",
    "scold",
    "panick",
    "legalistic",
)


def cell_ok(value: object) -> str:
    if value is True:
        return "ok"
    if value is False:
        return "FAIL"
    return "-"  # None: dimension not exercised (e.g. tier0-only scenario)


def verdict_for(s: dict) -> str:
    if s.get("errored"):
        return "INFRA"
    if s.get("passed"):
        return "PASS"
    if s.get("is_hard_failure"):
        return "HARD-FAIL"
    return "SOFT-FAIL"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"eval-summary: no report at {path}")
        print(
            "Generate one (free, no API, exercises harness machinery only):\n"
            "  cd apps/api && EVAL_DRY_RUN=1 uv run python -m evals.runner\n"
            "WARNING: that command OVERWRITES last-run.json -- if a paid gate's\n"
            "output might still be needed, copy the file somewhere first."
        )
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"eval-summary: could not read {path}: {exc}")
        return 2

    summary = payload.get("summary", {})
    scenarios = payload.get("scenarios", [])

    print(
        f"run: generated_at={payload.get('generated_at', '?')} "
        f"prompt={payload.get('prompt_version', '?')} "
        f"rubric={payload.get('rubric_version', '?')}"
    )
    print()

    headers = ("scenario", "category", "prefilter", "classify", "draft", "verdict", "cost")
    rows: list[tuple[str, ...]] = [headers]
    for s in scenarios:
        rows.append(
            (
                str(s.get("scenario_id", "?")),
                str(s.get("category", "?")),
                cell_ok(s.get("prefilter", {}).get("ok")),
                cell_ok(s.get("classification_ok")),
                cell_ok(s.get("draft_ok")),
                verdict_for(s),
                f"{s.get('cost_cents', 0.0):.2f}c",
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    for i, row in enumerate(rows):
        print(" | ".join(col.ljust(widths[j]) for j, col in enumerate(row)))
        if i == 0:
            print("-+-".join("-" * w for w in widths))

    print()
    print(
        f"GATE: {summary.get('passed', '?')}/{summary.get('total', '?')} passed | "
        f"hard-failed: {summary.get('hard_failed_scenario_ids', [])} | "
        f"soft-failed: {summary.get('soft_failed_scenario_ids', [])} | "
        f"errored (inconclusive, re-run): {summary.get('errored_scenario_ids', [])} | "
        f"release_blocked={summary.get('release_blocked', '?')}"
    )
    print(
        "      semantics: release_blocked = any hard failure OR any errored scenario; "
        "soft failures block prompt promotion, not development."
    )
    total_cost = sum(s.get("cost_cents", 0.0) for s in scenarios)
    print(f"      total run cost: {total_cost:.2f} cents")

    # --- Judge-inversion cross-check (gate-5 bug-class heuristic) ----------
    warnings = 0
    for s in scenarios:
        draft = s.get("draft") or {}
        failures = draft.get("failures") or []
        judge_failures = [f for f in failures if f.startswith("judge:")]
        if not judge_failures:
            continue
        reasoning = (draft.get("judge_reasoning") or "").lower()
        if any(f.upper().find("NO MATCHING KEY") != -1 for f in (jf.upper() for jf in judge_failures)):
            print(
                f"\nJUDGE-KEY-MISMATCH  {s.get('scenario_id')}: scorer reported "
                f"'NO MATCHING KEY' -- judge output-shape/eval-infra problem, "
                f"not a content miss. Failures: {judge_failures}"
            )
            warnings += 1
        if not any(kw in reasoning for kw in NEGATIVE_KEYWORDS):
            print(
                f"\nCHECK-INVERSION  {s.get('scenario_id')}: judge booleans failed "
                f"({judge_failures}) but judge_reasoning prose contains no negative "
                f"keywords -- suspect a judge verdict inversion (eval-infra bug, "
                f"see evals/judge.py 'BLOCKING bug found in gate 5 triage'). "
                f"Read the prose:\n  {draft.get('judge_reasoning', '')[:500]}"
            )
            warnings += 1
    if warnings == 0:
        print("\ninversion check: no judge-boolean-vs-prose disagreements flagged")

    return 1 if summary.get("release_blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
