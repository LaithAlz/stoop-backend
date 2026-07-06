"""Eval runner (#35) -- orchestrates real (or stubbed) Anthropic calls for
every scenario in ``evals/scenarios/`` and computes the release-blocker
gate.

************************************************************************
* THIS MODULE, RUN FOR REAL (``EVAL_DRY_RUN`` unset), HITS THE REAL      *
* ANTHROPIC API AND COSTS MONEY. It is exercised here ONLY via           *
* ``EVAL_DRY_RUN=1`` machinery tests in the default (non-``eval``       *
* -marked) test suite. Actually running the paid gate                   *
* (``uv run pytest -m eval`` or ``python -m evals.runner`` without       *
* ``EVAL_DRY_RUN=1``) is the ORCHESTRATOR's call, never something an     *
* implementer/agent does unilaterally.                                  *
************************************************************************

Why 3 samples + no temperature is a STRICTER bar, not a weaker one
-------------------------------------------------------------------
Issue #32 (and this repo's own docs) specify ``temperature=0`` for
``classify_severity``, and eval-scenarios-v1.md's scoring rule is "3
samples at temperature 0 -- flaky passes count as failures". But
``app/integrations/anthropic.py`` DELIBERATELY omits ``temperature``
entirely: on ``claude-sonnet-5`` the parameter is deprecated and the API
rejects any request that sets it (400 "temperature is deprecated for this
model" -- see that module's docstring "DETERMINISM NOTE"). There is
therefore no way to force literal token-level determinism from the API at
all for this model.

Consequence for THIS runner: the "3 samples, any flaky pass = failure"
rule was originally meant to catch bugs in an otherwise-deterministic
system (temp=0 should always agree with itself; 3 identical passes would
be the norm, and disagreement would flag a prompt/parsing bug). With
temperature omitted, the model's OWN natural sampling variance is now the
thing being policed -- which is a STRICTER bar than the rule's authors
likely pictured: a rubric+prompt pairing that is only "mostly" reliable at
a rubric-critical decision (e.g. the -10 C threshold, or a refusal flag)
will now fail this eval even though it might have passed a literal-
temperature=0 3x-identical check. That is the correct, conservative
outcome given the product's own bias rule ("never round down" / escalate
when uncertain) -- natural variance on an E-class/F-class scenario is
exactly the kind of unreliability the rubric is supposed to eliminate --
but it is worth naming explicitly: a "flaky" result here does not
necessarily mean the harness or prompt has a bug; it may mean the model's
sampling distribution genuinely straddles two answers on a borderline
message, which is itself useful, actionable signal (a rubric/prompt
-clarity gap) surfaced BY the stricter bar. This is a genuine consequence
of the ``claude-sonnet-5`` temperature deprecation discovered in this
issue's investigation, not something this task resolves unilaterally --
flagged in the issue report for the spec owner.

Why the "real call path" is reused private helpers, not new prompt code
--------------------------------------------------------------------------
"Call classify_severity's/draft_response's real call path" is honored by
reusing the SAME prompt/context-construction helpers those nodes use
internally (``app.agent.nodes.classify_severity._build_user_content``,
``app.agent.nodes.draft_response._build_user_content`` /
``_check_hard_guards`` / ``_violation_retry_note`` / ``_append_deferrals``
/ ``_GENERIC_SAFE_FALLBACK`` / ``_available_ack_chars`` /
``_length_retry_note`` / ``_LENGTH_BUDGET_CHARS`` -- the last three added
2026-07-05 alongside the node's own length-discipline fix, mirrored here
for the same reason: a stale mirror would silently keep failing the exact
length-budget scenarios the fix targets), the SAME frozen system prompts
(``get_classify_system_prompt`` / ``build_draft_system_prompt``), the SAME
tool schemas (``CLASSIFY_SEVERITY_TOOL`` / ``DRAFT_MESSAGE_TOOL``), and the
SAME transport function (``app.integrations.anthropic.call_tool_forced``)
production uses -- rather than hand-writing a second, parallel prompt
-construction path that could silently drift from what production actually
sends. The two node functions THEMSELVES (``classify_severity(state)`` /
``draft_response(state)``) are not called directly here because they are
tightly coupled to a live Postgres session (they SELECT the message row
and INSERT audit_log/drafts rows by ``message_id``/``case_id``) -- DB
plumbing this harness has no reason to exercise (it is already covered by
``tests/test_agent_classify_severity.py`` / ``tests/test_agent_draft_
response.py``, both DB-integration-marked). Reusing the private helpers
that build the ACTUAL prompt content (the substantive, drift-prone logic)
while reimplementing only the thin "call once, validate" wrapper inline
(a few lines) is the highest-fidelity path available without a database.

Design decision: draft_response is graded against GROUND-TRUTH
classification, not the sampled one
--------------------------------------------------------------------------
The draft call is fed ``expect.severity``/``rules_fired``/``modifier``/
``refusal_flags`` from the scenario's own ``expect`` block (see
``_ground_truth_severity_result``), never the possibly-wrong output of the
3 classification samples taken in step (b). This isolates "does this draft
correctly reflect a CORRECT classification" from "does this model classify
correctly" -- chaining them would conflate two independently-interesting
capabilities and make a draft failure ambiguous (is the prompt bad at
drafting, or did it inherit a bad classification?). This is a documented
runner design decision, not a doc ambiguity.

Release-blocker semantics (issue's requirement #3)
-------------------------------------------------------
Implemented in ``evals/scoring.py`` (``ScenarioResult.is_hard_failure`` /
``score_results``); summarized here per this task's explicit instruction:

- A scenario whose ``category`` is ``emergency`` or ``refusal`` (E-class /
  F-class) that fails ANY assertion (prefilter/classification/draft) is a
  HARD failure: ``GateVerdict.release_blocked`` becomes ``True``, and
  ``main()`` returns/exits non-zero.
- A scenario whose ``category`` is ``urgent`` or ``routine`` (U-class /
  R-class) that fails is a SOFT failure: recorded and reported (never
  silently dropped), but does NOT flip ``release_blocked`` -- per
  eval-scenarios-v1.md: "U/R misses block prompt promotion but not
  development."
- ANY prefilter (Tier-0) assertion failure is treated as hard-fail-worthy
  regardless of category -- see ``evals/scoring.py``'s module docstring for
  why (a Tier-0 regression, missed OR false-positive, is safety-critical
  either way).
- ``pytest -m eval`` gives per-scenario PASS/FAIL visibility (every
  scenario is its own parametrized test, so ANY assertion failure --
  hard or soft -- shows up as a red test in CI output/exit code, which is
  useful signal even for U/R misses). ``python -m evals.runner`` (this
  module's CLI) is the tool that encodes the DOC's precise blocking
  distinction as its own process exit code, independent of pytest's
  blanket "any failing test fails the run" behavior -- use IT, not raw
  pytest exit code, to decide whether a prompt/rubric change is mergeable.

Dry-run seam (``EVAL_DRY_RUN=1``)
--------------------------------------
Every real Anthropic call in this module goes through a ``ToolCaller``
value (``evals.types.ToolCaller`` -- matches ``call_tool_forced``'s
signature exactly). ``run_all``/``main`` select which ``ToolCaller`` to use
via a *factory* keyed on each scenario (dry-run mode needs the CURRENT
scenario's ``expect`` block to synthesize a "textbook-correct" answer, so
the factory -- not a single shared callable -- is the seam):

- Real mode (default): the factory returns ``anthropic_mod.call_tool_
  forced`` itself, unmodified -- money is spent, the true model is
  exercised.
- ``EVAL_DRY_RUN=1``: the factory returns ``make_dry_run_tool_caller
  (scenario)``, a deterministic stub with the IDENTICAL call signature that
  never touches the network and costs nothing. This is what
  ``tests/test_evals.py``'s default-suite tests use to exercise the
  loader -> prompt construction -> real assertion/scoring code ->
  reporting pipeline end-to-end. It proves the MACHINERY works; it proves
  nothing about whether the product's prompts/rubric are actually good at
  the task -- that is what the real, paid ``pytest -m eval`` gate is for,
  and this module never runs that gate on its own initiative.

Pacing + rate-limit backoff (round 2, diagnosed against the real paid
gate, 2026-07-05)
--------------------------------------------------------------------------
The real gate's first run burst ~100 calls back-to-back across the 20
-scenario corpus with the product client's own ``max_retries=0``
(``app/integrations/anthropic.py``: deliberately 0 so ITS retry-once
policy owns all retry behavior end-to-end -- see that module's docstring).
A 429 there kills a single ``call_tool_forced`` attempt instantly, which
this harness must not confuse with "the model produced a wrong answer".
Two independent mechanisms, both living ENTIRELY at the harness layer
(``wrap_with_pacing_and_backoff`` below) -- the product client itself is
NOT touched, per instruction:

1. **Inter-call pacing, round 2: token-budget-aware, not flat**
   (round 1's flat 1.5s ``PACE_SECONDS`` sleep was not enough: a SECOND
   paid-gate run, still on a fresh Tier-1 Anthropic account, still failed
   11/20 -- diagnosis: Tier-1 caps INPUT TOKENS per minute (~30-50k/min),
   not just request rate, and ``classify_severity``'s system prompt alone
   (the rubric embedded verbatim) is ~4k tokens; 6-7 classify calls alone
   can saturate a 25-30k/min budget regardless of how evenly spaced by
   time they are, and the window REFILLS every 60s, so no backoff ceiling
   under 60s can ever "outlast" it). :data:`EVAL_TOKEN_BUDGET_PER_MIN`
   (default 25000, conservative headroom under a real Tier-1 cap) is
   tracked against a sliding 60s window of ACTUAL ``tokens_in`` from
   completed calls (:class:`_TokenBudgetTracker`); before a new call, if
   the trailing sum PLUS a fixed per-tool ESTIMATE (this call's actual
   isn't known yet -- ``classify_severity`` ~4500, ``draft_message``
   ~2000, ``judge_draft`` ~1500, see
   :data:`_ESTIMATED_INPUT_TOKENS_BY_TOOL`) would exceed the budget,
   :func:`_wait_for_token_budget` sleeps until enough of the window ages
   out. A small, ALWAYS-applied :data:`PACE_FLOOR_SECONDS` (default 1s)
   sleep still runs first, independent of token accounting -- a
   request-RATE courtesy, not a token-budget one. Expected real-mode
   duration at the default 25k/min budget: roughly 12-18 minutes for the
   full 20-scenario corpus (dominated by token-budget waits, not the
   1s floor) -- see :func:`run_all`'s per-scenario progress line, which
   exists so this long a run doesn't look hung.

2. **Rate-limit/overload backoff, round 2: 70s ceiling, 6 retries**
   (:data:`RATE_LIMIT_MAX_RETRIES`, was 5; exponential 2s/4s/8s/16s/32s/
   64s, capped at :data:`RATE_LIMIT_BACKOFF_CAP_SECONDS`, was 30s, now 70s
   -- a full 60s window refill must fit inside a single backoff step, and
   70 > 60 with margin; the 2-base/6-retry sequence's own largest step,
   64s, already clears that bar without even touching the 70s cap) via
   :func:`_rate_limit_backoff_seconds`: when ``call_tool_forced`` raises
   ``AnthropicCallError`` whose CHAINED CAUSE (``exc.__cause__`` -- the
   product client always does ``raise AnthropicCallError(...) from exc``,
   see that module) is ``anthropic.RateLimitError`` (429) or
   ``anthropic.OverloadedError`` (529), sleep and retry the SAME call.
   Still narrow -- not every ``APIError`` subclass, see
   :data:`_RATE_LIMIT_CAUSES`.

CRITICAL DISTINCTION -- a 429 retry is NOT a second classification sample
--------------------------------------------------------------------------
The rubric's "3 samples, flaky = fail" rule (see above) polices the
MODEL's own natural variance across 3 calls that each successfully
produced an answer. A 429/529 retry never produced a model output at all
-- there is nothing to compare, nothing to count as "sample N disagreed".
Retrying it and using whichever attempt FINALLY succeeds is not a hidden
4th/5th "extra sample" and does not weaken the flaky-fails rule; it is
infrastructure plumbing making ONE of the mandated 3 samples possible in
the first place. The distinction that stays absolutely forbidden, still:
SEMANTIC retries (re-calling because the model's answer looked wrong) --
nothing in this module or ``app/agent/nodes/*`` ever does that; only a
transport-level rate-limit/overload failure is retried here, classified
strictly by exception type on the chained cause, never by inspecting the
(nonexistent, in this case) content of a response.

Infra failure vs semantic failure -- ``ScenarioInfraError``
--------------------------------------------------------------------------
Any OTHER ``AnthropicCallError`` (a timeout, a non-retryable 4xx/5xx, or a
retryable one whose backoff budget is exhausted) -- and any
``pydantic.ValidationError`` from a malformed tool response -- means this
scenario produced NO usable model output at all. ``run_scenario`` catches
both and sets ``ScenarioResult.infra_error`` instead of recording a
semantic failure: an errored scenario is INCONCLUSIVE (re-run it), never
counted as a flaky/failed classification or draft. See ``evals/scoring.
py``'s ``ScenarioResult.errored`` / ``GateVerdict.errored_scenario_ids``.

Latency accounting excludes pacing/backoff sleep (and reports retries)
--------------------------------------------------------------------------
``ClassificationSample.latency_s`` / ``DraftCheck.latency_s`` are meant to
reflect MODEL latency, not harness overhead. Every pacing/backoff sleep is
tracked on a module-level accountant (:data:`_pacing_accountant`); each
timed call site (:func:`_time_excluding_pacing`) subtracts whatever this
harness slept DURING that specific call from the wall-clock elapsed time
before recording it. This is correct only because ``run_all`` runs
scenarios strictly SEQUENTIALLY (documented above) -- there is never a
concurrent call whose sleep could be misattributed. The SAME accountant
also counts rate-limit/overload retries taken during that call
(``ClassificationSample.retries`` / ``DraftCheck.retries``) -- so the
always-written last-run report (below) can show "this scenario passed,
but needed 3 retries" as its own diagnostic signal, distinct from an
errored (retries-exhausted) scenario.

Always-written last-run report (round 2, diagnosed against the real paid
gate)
--------------------------------------------------------------------------
The first paid-gate diagnosis lost the per-sample ``AssertionError``
detail to a truncated (``| tail -8``) console capture -- ``main()`` now
ALWAYS writes the full per-scenario/per-sample payload to
:data:`LAST_RUN_PATH` (``evals/results/last-run.json``), unconditionally,
on every invocation (real or dry-run), regardless of ``--snapshot``. This
is distinct from :data:`SNAPSHOT_PATH` (``v1-baseline.json``), which stays
opt-in/deliberately-committed. Both share the same
``scenario_result_to_dict`` payload shape (severity/rules_fired/modifier/
refusal_flags/failures/retries/cost/latency per classification sample;
draft body/hard_guard_violations/judge_reasoning/failures/retries/cost/
latency for the draft) and the same ``.gitignore`` glob
(``evals/results/*.json``), so ``last-run.json`` is never accidentally
committed either.

Per-scenario progress line
--------------------------------------------------------------------------
At ~12-18 minutes for a full real-mode corpus run (see above), silence
looks like a hang. ``run_all`` prints one line per scenario as it
completes: index/total, scenario id, classification samples completed,
whether the draft ran, pass/fail/errored status, and the RUNNING
cumulative cost across the whole invocation so far.

``dry_run=True`` is a pure passthrough
--------------------------------------------------------------------------
``wrap_with_pacing_and_backoff(inner, dry_run=True)`` returns ``inner``
completely unwrapped -- no sleep, no retry, whatever ``inner`` does or
raises passes straight through. This keeps ``EVAL_DRY_RUN=1 python -m
evals.runner`` itself fast (not just pytest), and keeps every existing
dry-run test's zero-latency behavior unchanged by default (``run_scenario``
defaults to ``dry_run=True`` -- see its own docstring for the one footgun
this default creates and how call sites avoid it).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic
from pydantic import ValidationError

import app.agent.nodes.classify_severity as classify_severity_mod
import app.agent.nodes.draft_response as draft_response_mod
from app.agent import prefilter
from app.agent.prompts.v2 import (
    PROMPT_VERSION,
    build_draft_system_prompt,
    get_classify_system_prompt,
)
from app.agent.rubric import RUBRIC_VERSION
from app.agent.schemas import DraftResult, Severity, SeverityResult
from app.agent.tools import CLASSIFY_SEVERITY_TOOL, DRAFT_MESSAGE_TOOL
from app.integrations import anthropic as anthropic_mod
from evals.context import (
    heating_season_dict_for,
    map_refusal_flag,
    now_for,
    vulnerable_occupant_for,
    weather_snapshot_for,
)
from evals.fixtures import DEFAULT_VOICE_PROFILE
from evals.judge import judge_draft
from evals.scenario import Scenario, load_scenarios
from evals.scoring import (
    ClassificationSample,
    DraftCheck,
    GateVerdict,
    ScenarioResult,
    check_classification_sample,
    check_draft,
    expected_actions_for,
    score_results,
)
from evals.types import ToolCaller, ToolDict

SNAPSHOT_PATH: str = os.path.join(os.path.dirname(__file__), "results", "v1-baseline.json")
"""Git-tracked ONLY when the orchestrator deliberately commits a real gate
run's output (the root ``.gitignore`` excludes ``evals/results/*.json`` by
default, keeping ``.gitkeep`` so the directory itself still exists). Never
written unless ``--snapshot`` or ``EVAL_WRITE_SNAPSHOT=1`` is explicitly
set."""

LAST_RUN_PATH: str = os.path.join(os.path.dirname(__file__), "results", "last-run.json")
"""ALWAYS written by :func:`main`, every invocation (real or dry-run),
regardless of ``--snapshot`` -- see module docstring "Always-written
last-run report". Same ``.gitignore`` glob as :data:`SNAPSHOT_PATH`
(``evals/results/*.json``), so this is never accidentally committed
either; unlike ``SNAPSHOT_PATH`` it is never MEANT to be committed at all
-- purely scratch/operational, overwritten every run."""

# ---------------------------------------------------------------------------
# Pacing + rate-limit backoff -- see module docstring "Pacing + rate-limit
# backoff" / "Infra failure vs semantic failure" for the full rationale.
# ---------------------------------------------------------------------------

PACE_FLOOR_SECONDS: float = float(os.environ.get("EVAL_PACE_FLOOR_SECONDS", "1.0"))
"""Small, ALWAYS-applied minimum delay before every real-mode call --
independent of (and applied before) the token-budget wait below. A
request-rate courtesy, not a per-minute-token mechanism. Configurable via
``EVAL_PACE_FLOOR_SECONDS``, or per-call via :func:`wrap_with_pacing_and_
backoff`'s own ``pace_seconds`` parameter (what tests use)."""

TOKEN_WINDOW_SECONDS: float = 60.0
"""Anthropic's per-minute input-token cap window."""

EVAL_TOKEN_BUDGET_PER_MIN: int = int(os.environ.get("EVAL_TOKEN_BUDGET_PER_MIN", "25000"))
"""Conservative headroom under a real (diagnosed) Tier-1 per-minute INPUT
-token cap of ~30-50k -- see module docstring "Pacing + rate-limit backoff,
round 2". Configurable via the env var of the same name."""

_ESTIMATED_INPUT_TOKENS_BY_TOOL: dict[str, int] = {
    "classify_severity": 4500,
    "draft_message": 2000,
    "judge_draft": 1500,
}
_DEFAULT_TOKEN_ESTIMATE: int = 4500
"""Fallback estimate for a ``tool_name`` this map doesn't recognize --
conservative (same as classify_severity's, the largest of the three)."""

RATE_LIMIT_MAX_RETRIES: int = int(os.environ.get("EVAL_RATE_LIMIT_MAX_RETRIES", "6"))
"""Max backoff retries for a RATE-LIMIT/OVERLOAD cause specifically (see
:func:`_is_rate_limit_cause`) -- NOT a general "retry every failure"
budget. Configurable via ``EVAL_RATE_LIMIT_MAX_RETRIES``. Round 2: 5 -> 6."""

RATE_LIMIT_BACKOFF_BASE_SECONDS: float = 2.0
RATE_LIMIT_BACKOFF_CAP_SECONDS: float = 70.0
"""Round 2: 30s -> 70s -- a full 60s token-window refill must fit inside
ONE backoff step; 70 > 60 with margin (see module docstring)."""


def _rate_limit_backoff_seconds(attempt: int) -> float:
    """Exponential backoff, base 2s, capped at 70s: attempt 0->2s, 1->4s,
    2->8s, 3->16s, 4->32s, 5->64s (would be 128s uncapped)."""
    return min(RATE_LIMIT_BACKOFF_BASE_SECONDS * (2.0**attempt), RATE_LIMIT_BACKOFF_CAP_SECONDS)


_RATE_LIMIT_CAUSES: tuple[type[Exception], ...] = (
    anthropic.RateLimitError,
    anthropic.OverloadedError,
)
"""429 and 529 respectively -- see ``anthropic._exceptions``. Deliberately
narrow (not every ``APIError`` subclass): a genuine auth/bad-request error
retried repeatedly with backoff would just waste the whole budget on
something that will never succeed."""


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


_sleep: Callable[[float], Awaitable[None]] = _default_sleep
"""Seam -- mirrors ``app/integrations/anthropic.py``'s ``_now()`` pattern.
Tests monkeypatch ``evals.runner._sleep`` directly to a fake awaitable that
records/accounts requested durations WITHOUT a real ``asyncio.sleep``, so
the pacing/backoff retry logic is exercised deterministically with no real
wall-clock cost (this task's "fake clock" requirement)."""


def reset_sleep_for_tests() -> None:
    global _sleep
    _sleep = _default_sleep


@dataclass
class _PacingAccountant:
    """Tracks total seconds this harness has spent SLEEPING for pacing/
    backoff (never for the model itself), plus how many rate-limit/
    overload retries it has taken. Used only as a before/after DELTA
    around one call (see :func:`_time_excluding_pacing`) -- safe under this
    module's strictly-sequential execution (``run_all`` never runs
    scenarios concurrently), not safe under concurrent use."""

    total_slept_seconds: float = field(default=0.0)
    total_retries: int = field(default=0)


_pacing_accountant = _PacingAccountant()


def reset_pacing_accountant_for_tests() -> None:
    _pacing_accountant.total_slept_seconds = 0.0
    _pacing_accountant.total_retries = 0


async def _accounted_sleep(seconds: float) -> None:
    await _sleep(seconds)
    _pacing_accountant.total_slept_seconds += seconds


async def _time_excluding_pacing[T](coro: Awaitable[T]) -> tuple[T, float, int]:
    """Await *coro*, returning ``(result, elapsed_seconds, retries)``:
    ``elapsed_seconds`` EXCLUDES any pacing/backoff sleep this harness
    performed DURING the await, and ``retries`` counts how many rate-limit
    /overload backoff retries happened during it (both tracked via
    :data:`_pacing_accountant`) -- see module docstring "Latency accounting
    excludes pacing/backoff sleep (and reports retries)"."""
    slept_before = _pacing_accountant.total_slept_seconds
    retries_before = _pacing_accountant.total_retries
    start = time.monotonic()
    result = await coro
    wall_elapsed = time.monotonic() - start
    slept_during = _pacing_accountant.total_slept_seconds - slept_before
    retries_during = _pacing_accountant.total_retries - retries_before
    return result, max(0.0, wall_elapsed - slept_during), retries_during


@dataclass
class _TokenWindowEntry:
    timestamp: float
    tokens: int


@dataclass
class _TokenBudgetTracker:
    """Sliding 60s window of ACTUAL input tokens this harness has
    submitted, used to pace real-mode calls under a low Tier-1 per-minute
    cap -- see module docstring "Pacing + rate-limit backoff, round 2".
    Entries record ACTUAL ``tokens_in`` once a call SUCCEEDS; the value
    used for an UPCOMING call (whose actual isn't known yet) is always the
    fixed per-tool estimate -- see :data:`_ESTIMATED_INPUT_TOKENS_BY_TOOL`.
    """

    entries: list[_TokenWindowEntry] = field(default_factory=list)

    def _purge(self, *, now: float) -> None:
        cutoff = now - TOKEN_WINDOW_SECONDS
        self.entries = [e for e in self.entries if e.timestamp > cutoff]

    def trailing_sum(self, *, now: float) -> int:
        self._purge(now=now)
        return sum(e.tokens for e in self.entries)

    def record(self, *, now: float, tokens: int) -> None:
        self._purge(now=now)
        self.entries.append(_TokenWindowEntry(timestamp=now, tokens=tokens))

    def oldest_timestamp(self) -> float | None:
        return min((e.timestamp for e in self.entries), default=None)


_token_budget_tracker = _TokenBudgetTracker()


def reset_token_budget_tracker_for_tests() -> None:
    _token_budget_tracker.entries = []


async def _wait_for_token_budget(
    tool_name: str, *, budget: int = EVAL_TOKEN_BUDGET_PER_MIN
) -> None:
    """Block (via the fake-clock-friendly :func:`_accounted_sleep` seam)
    until the trailing-60s ACTUAL token sum plus this call's fixed
    ESTIMATE fits under *budget*. Loops (re-checking rather than computing
    one single sleep) since more than one stale entry may need to age out
    before there's enough headroom."""
    estimate = _ESTIMATED_INPUT_TOKENS_BY_TOOL.get(tool_name, _DEFAULT_TOKEN_ESTIMATE)
    while True:
        now = time.monotonic()
        current_sum = _token_budget_tracker.trailing_sum(now=now)
        if current_sum + estimate <= budget:
            return
        oldest = _token_budget_tracker.oldest_timestamp()
        if oldest is None:  # pragma: no cover -- defensive: nothing left to wait for
            return
        wait_seconds = max((oldest + TOKEN_WINDOW_SECONDS) - now, 0.1)
        await _accounted_sleep(wait_seconds)


class ScenarioInfraError(RuntimeError):
    """Raised when the harness's call layer cannot produce a model output
    at all: a rate-limit/overload backoff budget exhausted, or any OTHER
    ``AnthropicCallError`` (timeout, non-retryable API error). NEVER raised
    for a semantic/classification disagreement -- see module docstring
    "Infra failure vs semantic failure". ``run_scenario`` catches this
    (and ``pydantic.ValidationError``) and marks the scenario
    ``ScenarioResult.infra_error`` -- INCONCLUSIVE, re-run, never counted
    as a flaky/failed classification or draft."""


def _is_rate_limit_cause(exc: anthropic_mod.AnthropicCallError) -> bool:
    return isinstance(exc.__cause__, _RATE_LIMIT_CAUSES)


def _infra_error_message(exc: anthropic_mod.AnthropicCallError, *, retries_used: int) -> str:
    cause = exc.__cause__
    cause_name = type(cause).__name__ if cause is not None else "unknown"
    if _is_rate_limit_cause(exc):
        return (
            f"call infrastructure failed: rate-limit/overload backoff exhausted after "
            f"{retries_used} retr{'y' if retries_used == 1 else 'ies'} ({cause_name}): {exc}"
        )
    return (
        f"call infrastructure failed ({cause_name}, not a rate-limit/overload cause -- "
        f"not retried by this harness): {exc}"
    )


def wrap_with_pacing_and_backoff(
    inner: ToolCaller,
    *,
    dry_run: bool,
    pace_seconds: float = PACE_FLOOR_SECONDS,
    max_retries: int = RATE_LIMIT_MAX_RETRIES,
    token_budget: int = EVAL_TOKEN_BUDGET_PER_MIN,
) -> ToolCaller:
    """Wrap *inner* (the real transport function, the dry-run stub, or any
    test fake) with: a small pacing floor, token-budget-aware waiting, and
    rate-limit/overload backoff.

    ``dry_run=True`` returns *inner* completely UNCHANGED -- see module
    docstring "``dry_run=True`` is a pure passthrough". ``dry_run=False``:

    1. Sleeps ``pace_seconds`` (the small, always-applied courtesy floor).
    2. Waits (:func:`_wait_for_token_budget`) until the trailing-60s ACTUAL
       token sum plus this call's estimate fits under *token_budget* --
       ONCE, before the retry loop below, not re-checked per retry (a
       retry is still the SAME logical call).
    3. On an ``AnthropicCallError`` whose chained cause is a rate-limit/
       overload error (:func:`_is_rate_limit_cause`), sleeps with
       exponential backoff and retries the SAME call -- never counted as
       an extra classification sample (see module docstring "CRITICAL
       DISTINCTION"). Any other failure (non-retryable cause, or retries
       exhausted) raises :class:`ScenarioInfraError` instead of the
       original exception, so callers never need to re-derive "was this an
       infra failure?" from a raw ``AnthropicCallError``.
    4. On success, records the call's ACTUAL ``tokens_in`` into the token
       -budget tracker for future calls' pacing decisions.
    """
    if dry_run:
        return inner

    async def _paced(**kwargs: Any) -> anthropic_mod.ToolCallResult:
        tool_name = kwargs.get("tool_name", "")
        await _accounted_sleep(pace_seconds)
        await _wait_for_token_budget(tool_name, budget=token_budget)
        attempt = 0
        while True:
            try:
                result = await inner(**kwargs)
            except anthropic_mod.AnthropicCallError as exc:
                if not _is_rate_limit_cause(exc) or attempt >= max_retries:
                    raise ScenarioInfraError(
                        _infra_error_message(exc, retries_used=attempt)
                    ) from exc
                backoff = _rate_limit_backoff_seconds(attempt)
                attempt += 1
                _pacing_accountant.total_retries += 1
                await _accounted_sleep(backoff)
                continue
            _token_budget_tracker.record(now=time.monotonic(), tokens=result.tokens_in)
            return result

    return _paced


# ---------------------------------------------------------------------------
# Real call path -- classification (reuses classify_severity's own helpers)
# ---------------------------------------------------------------------------


async def _run_classification_sample(
    scenario: Scenario, *, sample_index: int, tool_caller: ToolCaller
) -> ClassificationSample:
    prefilter_result = prefilter.check(scenario.message)
    # Private-helper reuse -- see module docstring "Why the real call path...".
    user_content = classify_severity_mod._build_user_content(
        body=scenario.message,
        weather=weather_snapshot_for(scenario),
        vulnerable_occupant=vulnerable_occupant_for(scenario),
        heating_season=heating_season_dict_for(scenario),
        prefilter_result=prefilter_result,
        now=now_for(scenario),
    )

    call_result, latency, retries = await _time_excluding_pacing(
        tool_caller(
            system=get_classify_system_prompt(),
            user_content=user_content,
            tool=CLASSIFY_SEVERITY_TOOL,
            tool_name="classify_severity",
            timeout_seconds=anthropic_mod.CLASSIFICATION_BUDGET_SECONDS,
        )
    )

    severity_result = SeverityResult.model_validate(call_result.tool_input)
    failures = check_classification_sample(scenario, severity_result)
    cost_cents = anthropic_mod.estimate_cost_cents(
        tokens_in=call_result.tokens_in, tokens_out=call_result.tokens_out
    )
    return ClassificationSample(
        sample_index=sample_index,
        severity=severity_result.severity,
        rules_fired=severity_result.rules_fired,
        modifier=severity_result.modifier,
        refusal_flags=[flag.value for flag in severity_result.refusal_flags],
        reasoning=severity_result.reasoning,
        tokens_in=call_result.tokens_in,
        tokens_out=call_result.tokens_out,
        cost_cents=cost_cents,
        latency_s=latency,
        retries=retries,
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Real call path -- draft (reuses draft_response's own helpers)
# ---------------------------------------------------------------------------

_GROUND_TRUTH_REASONING: list[str] = ["(ground-truth scenario expectation, not model output)"]


def _ground_truth_severity_result(scenario: Scenario) -> SeverityResult:
    """See module docstring "Design decision: draft_response is graded
    against GROUND-TRUTH classification"."""
    modifier: str | None = None
    if scenario.expect.modifier == "vulnerable_occupant":
        occupant = scenario.context.tenant.vulnerable_occupant or "vulnerable occupant"
        modifier = f"vulnerable-occupant bump: {occupant}"
    refusal_flags = [map_refusal_flag(name) for name in scenario.expect.refusal_flags]
    return SeverityResult(
        severity=Severity(scenario.expect.severity.upper()),
        rules_fired=list(scenario.expect.rules_fired),
        modifier=modifier,
        refusal_flags=refusal_flags,
        reasoning=list(_GROUND_TRUTH_REASONING),
    )


async def _run_draft(scenario: Scenario, *, tool_caller: ToolCaller) -> DraftCheck:
    severity_result = _ground_truth_severity_result(scenario)
    # Private-helper reuse -- see module docstring "Why the real call path...".
    base_user_content = draft_response_mod._build_user_content(
        body=scenario.message,
        tenant_name=scenario.context.tenant.name,
        house_rules=None,  # not modeled by eval-scenarios-v1.md's context block
        severity_result=severity_result,
        refusal_flags=severity_result.refusal_flags,
    )
    system_prompt = build_draft_system_prompt(DEFAULT_VOICE_PROFILE)

    # Documented exception (draft_response.py's own module docstring
    # "Length discipline") -- a refusal-flagged scenario never has its
    # length enforced; the mandated deferral legitimately makes it longer.
    enforce_length_budget = not severity_result.refusal_flags
    available_chars = draft_response_mod._available_ack_chars(severity_result.refusal_flags)

    ack_body: str | None = None
    last_candidate_body: str | None = None
    last_violations: list[str] = []
    user_content = base_user_content
    total_tokens_in = 0
    total_tokens_out = 0
    total_latency = 0.0
    total_retries = 0

    # Mirrors draft_response.py's own "one retry on hard-guard OR length
    # violation" loop exactly -- see module docstring for why the node's
    # private helpers, not a hand-rolled equivalent, are reused here.
    for _attempt in range(2):
        call_result, call_latency, call_retries = await _time_excluding_pacing(
            tool_caller(
                system=system_prompt,
                user_content=user_content,
                tool=DRAFT_MESSAGE_TOOL,
                tool_name="draft_message",
                timeout_seconds=anthropic_mod.CLASSIFICATION_BUDGET_SECONDS,
            )
        )
        total_latency += call_latency
        total_retries += call_retries
        total_tokens_in += call_result.tokens_in
        total_tokens_out += call_result.tokens_out

        candidate = DraftResult.model_validate(call_result.tool_input)
        violations = draft_response_mod._check_hard_guards(body=candidate.body)
        too_long = (
            enforce_length_budget and len(candidate.body) > draft_response_mod._LENGTH_BUDGET_CHARS
        )
        last_candidate_body = candidate.body
        last_violations = violations

        if not violations and not too_long:
            ack_body = candidate.body
            break

        if violations:
            user_content = base_user_content + draft_response_mod._violation_retry_note(violations)
        else:
            user_content = base_user_content + draft_response_mod._length_retry_note(
                available_chars
            )

    guard_failed = False
    length_over_budget = False
    if ack_body is None:
        if last_candidate_body is not None and not last_violations:
            # Guards were clean -- the ONLY remaining problem was length.
            # TRUNCATION IS FORBIDDEN: keep the long draft, flag it instead
            # (mirrors draft_response.py's own post-loop decision exactly).
            ack_body = last_candidate_body
            length_over_budget = True
        else:
            guard_failed = True
            ack_body = draft_response_mod._GENERIC_SAFE_FALLBACK

    final_body = draft_response_mod._append_deferrals(ack_body, severity_result.refusal_flags)

    (judge_verdict, judge_call_result), judge_latency, judge_retries = await _time_excluding_pacing(
        judge_draft(
            draft_body=final_body,
            must_include=scenario.expect.draft_must_include,
            must_not_include=scenario.expect.draft_must_not_include,
            tool_caller=tool_caller,
        )
    )
    total_latency += judge_latency
    total_retries += judge_retries
    total_tokens_in += judge_call_result.tokens_in
    total_tokens_out += judge_call_result.tokens_out

    failures = check_draft(
        scenario,
        draft_body=final_body,
        hard_guard_violations=last_violations,
        guard_failed=guard_failed,
        judge_verdict=judge_verdict,
    )
    cost_cents = anthropic_mod.estimate_cost_cents(
        tokens_in=total_tokens_in, tokens_out=total_tokens_out
    )
    return DraftCheck(
        draft_body=final_body,
        ack_body=ack_body,
        hard_guard_violations=last_violations,
        guard_failed=guard_failed,
        judge_reasoning=judge_verdict.reasoning,
        tokens_in=total_tokens_in,
        tokens_out=total_tokens_out,
        cost_cents=cost_cents,
        latency_s=total_latency,
        retries=total_retries,
        failures=failures,
        length_over_budget=length_over_budget,
    )


# ---------------------------------------------------------------------------
# Per-scenario / all-scenarios orchestration
# ---------------------------------------------------------------------------

CLASSIFICATION_SAMPLES_PER_SCENARIO: int = 3
"""eval-scenarios-v1.md: "Run matrix per scenario: 3 samples ... flaky
passes count as failures." See module docstring for why this is a
stricter bar than it sounds now that temperature is omitted."""


async def run_scenario(
    scenario: Scenario, *, tool_caller: ToolCaller, dry_run: bool = True
) -> ScenarioResult:
    """Run every assertion this harness owns for one scenario:

    (a) Tier-0 prefilter check (pure function, always run, free -- never
        paced/backed-off, it makes no network call).
    (b) 3 independent classify_severity samples (skipped entirely for
        ``tier0_only`` negative-prefilter scenarios -- see
        ``evals/scenario.py``'s docstring on that field).
    (c) One draft_response call + one LLM-judge call (also skipped for
        ``tier0_only`` scenarios).

    *tool_caller* is wrapped with :func:`wrap_with_pacing_and_backoff`
    before (b)/(c) run, controlled by *dry_run* -- NOT inferred from
    *tool_caller* itself. ``dry_run`` DEFAULTS TO ``True`` (safe: no
    pacing/backoff sleep unless a caller explicitly opts in), which keeps
    every existing dry-run-stub call site fast without modification --
    but this creates ONE footgun: passing the REAL transport function
    (``make_real_tool_caller``/``anthropic_mod.call_tool_forced``) while
    leaving ``dry_run`` at its default would silently skip pacing/backoff
    against the real API. The two real-mode call sites in this codebase
    (``main()`` and ``tests/test_evals.py``'s ``test_eval_scenario``) both
    pass ``dry_run`` explicitly, computed from the SAME ``EVAL_DRY_RUN``
    check that selects the tool_caller itself, so the two can never
    disagree there.

    A ``ScenarioInfraError`` (rate-limit/overload backoff exhausted, or any
    other call failure) or a ``pydantic.ValidationError`` (malformed model
    output) raised anywhere in (b)/(c) is caught HERE and recorded as
    ``result.infra_error`` -- the scenario is INCONCLUSIVE, never scored as
    a semantic pass/fail. See module docstring "Infra failure vs semantic
    failure".
    """
    prefilter_result = prefilter.check(scenario.message)
    prefilter_ok: bool | None = None
    if scenario.prefilter_must_fire is not None:
        prefilter_ok = prefilter_result.hard_hit is scenario.prefilter_must_fire

    result = ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        prefilter_result=prefilter_result,
        prefilter_expected=scenario.prefilter_must_fire,
        prefilter_ok=prefilter_ok,
    )

    if scenario.tier0_only:
        return result

    paced_caller = wrap_with_pacing_and_backoff(tool_caller, dry_run=dry_run)

    try:
        samples: list[ClassificationSample] = []
        for i in range(CLASSIFICATION_SAMPLES_PER_SCENARIO):
            samples.append(
                await _run_classification_sample(scenario, sample_index=i, tool_caller=paced_caller)
            )
        result.classification_samples = samples
        result.draft_check = await _run_draft(scenario, tool_caller=paced_caller)
    except ScenarioInfraError as exc:
        result.infra_error = str(exc)
    except ValidationError as exc:
        result.infra_error = f"malformed model output (schema validation failed): {exc}"

    return result


def _progress_line(
    *,
    index: int,
    total: int,
    scenario: Scenario,
    result: ScenarioResult,
    cumulative_cost_cents: float,
) -> str:
    if result.errored:
        status = "ERRORED"
    elif result.passed:
        status = "passed"
    elif result.is_hard_failure:
        status = "HARD-FAIL"
    else:
        status = "soft-fail"
    samples_done = len(result.classification_samples)
    samples_total = 0 if scenario.tier0_only else CLASSIFICATION_SAMPLES_PER_SCENARIO
    return (
        f"[{index}/{total}] {scenario.id}: samples_done={samples_done}/{samples_total} "
        f"draft_done={result.draft_check is not None} status={status} "
        f"cumulative_cost=${cumulative_cost_cents / 100:.4f}"
    )


async def run_all(
    scenarios: list[Scenario],
    *,
    tool_caller_factory: Callable[[Scenario], ToolCaller],
    dry_run: bool = True,
) -> list[ScenarioResult]:
    """Run every scenario SEQUENTIALLY (not concurrently) -- deliberate:
    keeps cost/latency accounting per-scenario unambiguous, is
    rate-limit-friendly against the real API (see module docstring
    "Pacing + rate-limit backoff"), and is a precondition for
    :func:`_time_excluding_pacing`'s accountant-diff correctness.

    Prints one progress line per scenario as it completes (id, samples
    done, whether the draft ran, status, RUNNING cumulative cost) -- see
    module docstring "Per-scenario progress line". A full real-mode corpus
    run takes roughly 12-18 minutes at the default token budget; without
    this, a quiet terminal for that long looks indistinguishable from a
    hang.
    """
    results: list[ScenarioResult] = []
    cumulative_cost_cents = 0.0
    total = len(scenarios)
    for index, scenario in enumerate(scenarios, start=1):
        tool_caller = tool_caller_factory(scenario)
        result = await run_scenario(scenario, tool_caller=tool_caller, dry_run=dry_run)
        results.append(result)
        cumulative_cost_cents += result.total_cost_cents
        print(  # noqa: T201 -- operator-visible progress line, this task's requirement #3
            _progress_line(
                index=index,
                total=total,
                scenario=scenario,
                result=result,
                cumulative_cost_cents=cumulative_cost_cents,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Dry-run stub -- see module docstring "Dry-run seam"
# ---------------------------------------------------------------------------


def _dry_run_classification_input(scenario: Scenario) -> dict[str, Any]:
    modifier: str | None = None
    if scenario.expect.modifier == "vulnerable_occupant":
        modifier = "vulnerable-occupant bump: dry-run stub"
    return {
        "severity": scenario.expect.severity.upper(),
        "rules_fired": list(scenario.expect.rules_fired),
        "modifier": modifier,
        "refusal_flags": [map_refusal_flag(name).value for name in scenario.expect.refusal_flags],
        "reasoning": ["dry-run stub reasoning"],
    }


def _dry_run_draft_input(scenario: Scenario) -> dict[str, Any]:
    body = " ".join(scenario.expect.draft_must_include) or (
        "Thanks for letting me know -- I will follow up soon."
    )
    return {"body": body, "refusal_templates_used": []}


def _dry_run_judge_input(scenario: Scenario) -> dict[str, Any]:
    return {
        "must_include_present": dict.fromkeys(scenario.expect.draft_must_include, True),
        "must_not_include_absent": dict.fromkeys(scenario.expect.draft_must_not_include, True),
        "plain_language_conformant": True,
        "reasoning": "dry-run stub verdict: happy path (see evals/runner.py docstring).",
    }


def make_dry_run_tool_caller(scenario: Scenario) -> ToolCaller:
    """A deterministic, scenario-aware stub with ``call_tool_forced``'s
    EXACT signature (see ``evals/types.py``). Synthesizes a "textbook
    -correct" tool_input for whichever ``tool_name`` is requested, drawn
    directly from *scenario*'s own ``expect`` block. Proves the machinery
    (loader -> prompt construction -> assertion -> scoring -> reporting)
    end-to-end with zero network calls and zero cost -- see module
    docstring."""

    async def _caller(
        *,
        system: str,
        user_content: str,
        tool: ToolDict,
        tool_name: str,
        max_tokens: int = 1024,
        timeout_seconds: float = 0.0,
    ) -> anthropic_mod.ToolCallResult:
        del system, user_content, tool, max_tokens, timeout_seconds  # unused by the stub
        if tool_name == "classify_severity":
            tool_input = _dry_run_classification_input(scenario)
        elif tool_name == "draft_message":
            tool_input = _dry_run_draft_input(scenario)
        elif tool_name == "judge_draft":
            tool_input = _dry_run_judge_input(scenario)
        else:  # pragma: no cover -- defensive; every real tool_name is one of the three above
            raise AssertionError(f"dry-run stub has no fixture for tool_name={tool_name!r}")
        return anthropic_mod.ToolCallResult(
            tool_input=tool_input, tokens_in=100, tokens_out=50, model="dry-run-stub"
        )

    return _caller


def make_real_tool_caller(scenario: Scenario) -> ToolCaller:
    """Real mode: every scenario shares the same, unmodified transport
    function -- the scenario argument only exists so this has the same
    signature as :func:`make_dry_run_tool_caller` for the factory seam."""
    del scenario
    return anthropic_mod.call_tool_forced


def tool_caller_factory(*, dry_run: bool) -> Callable[[Scenario], ToolCaller]:
    return make_dry_run_tool_caller if dry_run else make_real_tool_caller


# ---------------------------------------------------------------------------
# Reporting + snapshot
# ---------------------------------------------------------------------------


def _classification_sample_to_dict(sample: ClassificationSample) -> dict[str, Any]:
    return {
        "sample_index": sample.sample_index,
        "severity": sample.severity.value,
        "rules_fired": sample.rules_fired,
        "modifier": sample.modifier,
        "refusal_flags": sample.refusal_flags,
        # Derived, not independently model-graded -- see
        # evals/scoring.py's expected_actions_for docstring.
        "derived_actions": expected_actions_for(
            sample.severity, has_refusal_flags=bool(sample.refusal_flags)
        ),
        "cost_cents": sample.cost_cents,
        "latency_s": sample.latency_s,
        "retries": sample.retries,
        "ok": sample.ok,
        "failures": sample.failures,
    }


def _draft_check_to_dict(draft_check: DraftCheck) -> dict[str, Any]:
    return {
        "draft_body": draft_check.draft_body,
        "hard_guard_violations": draft_check.hard_guard_violations,
        "guard_failed": draft_check.guard_failed,
        "length_over_budget": draft_check.length_over_budget,
        "judge_reasoning": draft_check.judge_reasoning,
        "cost_cents": draft_check.cost_cents,
        "latency_s": draft_check.latency_s,
        "retries": draft_check.retries,
        "ok": draft_check.ok,
        "failures": draft_check.failures,
    }


def scenario_result_to_dict(result: ScenarioResult) -> dict[str, Any]:
    return {
        "scenario_id": result.scenario_id,
        "category": result.category,
        "hard_fail_class": result.hard_fail_class,
        "prefilter": {
            "hard_hit": result.prefilter_result.hard_hit,
            "categories": result.prefilter_result.categories,
            "guards": result.prefilter_result.guards,
            "expected": result.prefilter_expected,
            "ok": result.prefilter_ok,
        },
        "classification_samples": [
            _classification_sample_to_dict(s) for s in result.classification_samples
        ],
        "classification_ok": result.classification_ok,
        "draft": _draft_check_to_dict(result.draft_check) if result.draft_check else None,
        "draft_ok": result.draft_ok,
        "cost_cents": result.total_cost_cents,
        "latency_s": result.total_latency_s,
        "passed": result.passed,
        "is_hard_failure": result.is_hard_failure,
        "errored": result.errored,
        "infra_error": result.infra_error,
        "top_level_failures": result.top_level_failures,
    }


def write_snapshot(
    results: list[ScenarioResult], verdict: GateVerdict, *, path: str = SNAPSHOT_PATH
) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "prompt_version": PROMPT_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "scenarios": [scenario_result_to_dict(r) for r in results],
        "summary": {
            "total": verdict.total,
            "passed": verdict.passed,
            "failed": verdict.failed,
            "hard_failed_scenario_ids": verdict.hard_failed_scenario_ids,
            "soft_failed_scenario_ids": verdict.soft_failed_scenario_ids,
            "errored_scenario_ids": verdict.errored_scenario_ids,
            "release_blocked": verdict.release_blocked,
        },
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def write_last_run_report(
    results: list[ScenarioResult], verdict: GateVerdict, *, path: str = LAST_RUN_PATH
) -> None:
    """Thin, intent-naming wrapper around :func:`write_snapshot` -- same
    rich per-scenario/per-sample payload (severity/rules_fired/modifier/
    refusal_flags/failures/retries/cost/latency per classification sample;
    draft body/hard_guard_violations/judge_reasoning/failures/retries/cost/
    latency for the draft), written UNCONDITIONALLY by :func:`main` so a
    truncated console (e.g. ``| tail -8``) never loses the detail needed to
    diagnose a batch failure -- see module docstring "Always-written
    last-run report"."""
    write_snapshot(results, verdict, path=path)


def format_report(results: list[ScenarioResult], verdict: GateVerdict) -> str:
    lines: list[str] = []
    for result in results:
        if result.errored:
            marker = "ERROR (inconclusive -- re-run)"
        elif result.passed:
            marker = "PASS"
        elif result.is_hard_failure:
            marker = "HARD-FAIL"
        else:
            marker = "SOFT-FAIL"
        lines.append(
            f"[{marker}] {result.scenario_id} (category={result.category}) "
            f"prefilter_ok={result.prefilter_ok} classification_ok={result.classification_ok} "
            f"draft_ok={result.draft_ok} cost_cents={result.total_cost_cents:.4f} "
            f"latency_s={result.total_latency_s:.2f}"
        )
        if result.infra_error:
            lines.append(f"    - INFRA (not a semantic miss): {result.infra_error}")
        for failure in result.top_level_failures:
            lines.append(f"    - {failure}")
        for sample in result.classification_samples:
            for failure in sample.failures:
                lines.append(f"    - [sample {sample.sample_index}] {failure}")
        if result.draft_check is not None:
            for failure in result.draft_check.failures:
                lines.append(f"    - [draft] {failure}")
    lines.append(
        f"\n{verdict.passed}/{verdict.total} passed | "
        f"hard-failed: {verdict.hard_failed_scenario_ids} | "
        f"soft-failed: {verdict.soft_failed_scenario_ids} | "
        f"errored (inconclusive, re-run): {verdict.errored_scenario_ids} | "
        f"release_blocked={verdict.release_blocked}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_REAL_RUN_WARNING: str = (
    "\n*** EVAL_DRY_RUN is not set -- this run will call the REAL Anthropic API and "
    "cost real money. This is the orchestrator's paid gate, not something to run "
    "casually. Set EVAL_DRY_RUN=1 to exercise the harness for free. ***\n"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help=(
            f"ALSO write a baseline snapshot to {SNAPSHOT_PATH} (the last-run "
            f"report at {LAST_RUN_PATH} is always written regardless)."
        ),
    )
    parser.add_argument(
        "--no-negative",
        dest="include_negative",
        action="store_false",
        default=True,
        help="Skip the negative Tier-0 prefilter suite.",
    )
    args = parser.parse_args(argv)

    dry_run = os.environ.get("EVAL_DRY_RUN") == "1"
    if not dry_run:
        print(_REAL_RUN_WARNING, file=sys.stderr)

    scenarios = load_scenarios(include_tier0_only=args.include_negative)
    factory = tool_caller_factory(dry_run=dry_run)
    results = asyncio.run(run_all(scenarios, tool_caller_factory=factory, dry_run=dry_run))
    verdict = score_results(results)

    print(format_report(results, verdict))

    # ALWAYS written, regardless of --snapshot -- see module docstring
    # "Always-written last-run report". Pass `path=` explicitly (a fresh
    # global lookup at call time) rather than relying on the function's own
    # default parameter, which is bound once at module-import time -- tests
    # that monkeypatch `evals.runner.LAST_RUN_PATH`/`SNAPSHOT_PATH` need
    # THIS call site to see the patched value, not the value the name held
    # when this module first loaded.
    write_last_run_report(results, verdict, path=LAST_RUN_PATH)
    print(f"\nLast-run report (always written): {LAST_RUN_PATH}")

    if args.snapshot or os.environ.get("EVAL_WRITE_SNAPSHOT") == "1":
        write_snapshot(results, verdict, path=SNAPSHOT_PATH)
        print(f"\nSnapshot written to {SNAPSHOT_PATH}")

    return 1 if verdict.release_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__: list[str] = [
    "CLASSIFICATION_SAMPLES_PER_SCENARIO",
    "EVAL_TOKEN_BUDGET_PER_MIN",
    "LAST_RUN_PATH",
    "PACE_FLOOR_SECONDS",
    "RATE_LIMIT_BACKOFF_BASE_SECONDS",
    "RATE_LIMIT_BACKOFF_CAP_SECONDS",
    "RATE_LIMIT_MAX_RETRIES",
    "SNAPSHOT_PATH",
    "TOKEN_WINDOW_SECONDS",
    "ScenarioInfraError",
    "format_report",
    "main",
    "make_dry_run_tool_caller",
    "make_real_tool_caller",
    "reset_pacing_accountant_for_tests",
    "reset_sleep_for_tests",
    "reset_token_budget_tracker_for_tests",
    "run_all",
    "run_scenario",
    "scenario_result_to_dict",
    "tool_caller_factory",
    "wrap_with_pacing_and_backoff",
    "write_last_run_report",
    "write_snapshot",
]
