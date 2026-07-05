"""Anthropic client factory + token/cost accounting helper (#26/#30-series).

Per ``docs/03-engineering/architecture.md`` §4's layout table: "client
factory, token/cost accounting helper". Every LLM-calling agent node
(``classify_intent``, ``classify_severity``, ``draft_response``) goes
through :func:`call_tool_forced` here rather than constructing its own
``AsyncAnthropic`` client or handling ``tool_choice``/timeout plumbing
independently — one call site for the SDK, matching the "single call site"
pattern already used by ``app/agent/emergency.py`` and
``app/integrations/twilio.py``.

Model
-----
``architecture.md`` §5 specifies only "Anthropic API (Claude)" generically
— it does not pin a literal model id string anywhere in the repo. Per this
issue's explicit instruction, the newest Sonnet-tier model
(``claude-sonnet-5``) is used rather than an older pinned snapshot. This is
a genuine doc gap: ``architecture.md`` should record the concrete model id
once it is confirmed against a real Anthropic account (flagged in the
issue report, not resolved unilaterally here).

Budget / retry — 20s END-TO-END, not per-attempt (spec-guardian ruling,
2026-07-05)
--------------------------------------------------------------------------
``docs/02-product/emergency-prefilter.md``'s "Classification budget: 20
seconds end-to-end ... on timeout, API error, or hard failure after one
retry" governs literally: ONE shared 20-second deadline covers the initial
attempt AND its single retry TOGETHER, never 20 seconds per attempt (an
earlier revision of this module misread it as per-attempt; corrected here
per the spec-guardian's ruling). Callers establish ONE deadline at node
entry (:func:`new_deadline`) and derive each attempt's own timeout from it
(:func:`attempt_timeout`):

- **First attempt** is capped at :data:`FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS`
  (12s) even though the full 20s remains at that point — a slow-but-not
  -yet-timed-out first call must never be allowed to silently consume the
  ENTIRE budget and leave nothing for the retry it's supposed to share the
  deadline with.
- **Retry** gets whatever remains of the original 20s deadline (typically
  ~8s when the first attempt used its full 12s cap). If that remainder is
  below :data:`MIN_RETRY_BUDGET_SECONDS` (2s) there is no meaningful time
  left for a second network round-trip: ``attempt_timeout`` returns
  ``None`` and the caller skips the retry entirely, going straight to its
  own degraded-mode handling — exactly as if the retry had also failed,
  just without wasting the last scraps of the budget on a doomed attempt.

:func:`call_tool_forced` itself still makes exactly ONE attempt, at
whatever ``timeout_seconds`` it's given, and raises
:class:`AnthropicCallError` on any failure (timeout, API error, or a
forced tool_choice response that somehow carries no ``tool_use`` block) —
it has NO knowledge of the shared deadline or the retry policy itself.
Each calling node owns the attempt-loop (different nodes react
differently to a double failure; see e.g.
``app/agent/nodes/classify_severity.py``'s module docstring for its
degraded-mode seam) — this module only supplies the shared arithmetic
(:func:`new_deadline` / :func:`attempt_timeout`) so the 20s split can't
drift between the three callers.

Reported gap: "direct SDK, no wrapper" vs. this module (spec-guardian
ruling, 2026-07-05 — code stands)
--------------------------------------------------------------------------
``architecture.md`` §5 / ``apps/api/CLAUDE.md`` both describe
``classify_severity`` as calling "the Anthropic SDK directly ... (not a
wrapper)". This module is, literally, a thin wrapper around
``client.messages.create``. The spec-guardian's ruling: the INTENT behind
"no wrapper" is that no framework may hide the rubric prompt or let a node
delegate its OWN prompt-construction or tool-choice decisions to shared
code — every node still builds its own system/user content and picks its
own tool verbatim; nothing here decides WHAT to ask the model.
:func:`call_tool_forced` owns TRANSPORT ONLY — the ``asyncio.wait_for``
timeout, the forced ``tool_choice``/``tools`` plumbing, and parsing the
response into a plain :class:`ToolCallResult` — never a prompt or
classification decision. Code stands as written; the docs wording ("no
wrapper") will be amended in a future docs pass to state this distinction
explicitly. Noted here so the tension isn't silently papered over in the
meantime.

Sends tenant message content to Anthropic BY DESIGN
------------------------------------------------------
Every call this module makes carries the tenant's message text (and other
case context) to the Anthropic API as the whole point of the product
(classification and drafting both require reading what the tenant wrote).
This is the ONE place in the codebase where message content deliberately
leaves the process boundary to a third party — never-break rule #5 ("never
log message bodies") still applies to OUR OWN logs/Sentry/structlog calls;
it does not (and cannot) apply to the Anthropic request payload itself.

Cost accounting
-----------------
:func:`estimate_cost_cents` uses a small, hardcoded, CONSERVATIVE pricing
table (:data:`_INPUT_PRICE_PER_MTOK_USD` / :data:`_OUTPUT_PRICE_PER_MTOK_USD`)
rather than calling a pricing API (none exists) or reading Anthropic's
billing dashboard (no account access from this environment). The values
mirror Anthropic's long-standing published Sonnet-tier rate ($3.00 / MTok
input, $15.00 / MTok output — unchanged across Claude 3.5/4/4.5 Sonnet per
the public pricing page as last verified before this module's cutoff) —
there is no independently-confirmed published rate specific to
"claude-sonnet-5" available in this environment. Treat this constant as a
placeholder to reconcile with the actual invoiced rate once real billing
data exists (flagged in the issue report); erring high (rather than
guessing low) is the conservative choice per the same bias-rule spirit the
rubric applies elsewhere. Prompt caching (#70) is out of scope — every
token is priced at the plain (non-cached) rate.

No feature-flag reads here (CLAUDE.md agent rules): this module is called
from ``agent/`` node code and must behave identically regardless of any
flag service's availability.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, cast

import anthropic
import structlog
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam, ToolUseBlock

from app.config import settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Model + budget constants
# ---------------------------------------------------------------------------

MODEL: str = "claude-sonnet-5"
"""The Anthropic model id used for every agent LLM call. See module
docstring "Model" for why this literal isn't sourced from architecture.md."""

CLASSIFICATION_BUDGET_SECONDS: float = 20.0
"""The END-TO-END budget shared by an initial attempt + its one retry —
see module docstring "Budget / retry". NOT a per-attempt timeout; use
:func:`new_deadline` / :func:`attempt_timeout` to split it correctly."""

FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS: float = 12.0
"""Cap on the FIRST attempt's timeout even when the full budget remains —
see module docstring "Budget / retry"."""

MIN_RETRY_BUDGET_SECONDS: float = 2.0
"""Below this much remaining budget, a retry is skipped entirely (not
attempted with a near-zero timeout) — see module docstring "Budget /
retry"."""


def _now() -> float:
    """Monotonic clock seam — tests monkeypatch this directly to simulate
    elapsed time deterministically, with no real sleeps (mirrors
    ``app/integrations/weather.py``'s ``_now()`` pattern)."""
    return time.monotonic()


def new_deadline() -> float:
    """Return an absolute (``time.monotonic()``-based) deadline
    :data:`CLASSIFICATION_BUDGET_SECONDS` from now — the ONE shared budget
    a node's initial attempt and its single retry both draw from. Call
    once, at node entry, and pass the same value to every
    :func:`attempt_timeout` call for that node invocation."""
    return _now() + CLASSIFICATION_BUDGET_SECONDS


def attempt_timeout(deadline: float, *, is_retry: bool) -> float | None:
    """Return the ``timeout_seconds`` to use for ONE attempt against the
    shared *deadline* (from :func:`new_deadline`), or ``None`` when a retry
    should be skipped entirely because the budget is effectively exhausted.

    See module docstring "Budget / retry" for the full rationale:

    - ``is_retry=False`` (the first attempt): capped at
      :data:`FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS` even when more of the
      budget remains.
    - ``is_retry=True``: gets whatever remains of *deadline*. Returns
      ``None`` when that remainder is below :data:`MIN_RETRY_BUDGET_SECONDS`
      — callers must treat ``None`` as "skip the retry, go straight to
      degraded/fallback handling", not as "retry with ~0 timeout".
    """
    remaining = deadline - _now()
    if not is_retry:
        return max(0.0, min(remaining, FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS))
    if remaining < MIN_RETRY_BUDGET_SECONDS:
        return None
    return remaining


# ---------------------------------------------------------------------------
# Pricing constants — see module docstring "Cost accounting"
# ---------------------------------------------------------------------------

_PRICING_SOURCE_NOTE: str = (
    "Anthropic published Sonnet-tier pricing ($3.00/$15.00 per MTok "
    "input/output), last verified prior to this module's knowledge "
    "cutoff -- no rate specific to claude-sonnet-5 was independently "
    "confirmable in this environment. Conservative placeholder; "
    "reconcile with real billing data once available."
)

_INPUT_PRICE_PER_MTOK_USD: float = 3.00
_OUTPUT_PRICE_PER_MTOK_USD: float = 15.00


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """Return the process-wide ``AsyncAnthropic`` client, created lazily.

    A single client is reused across calls (connection pooling); tests
    monkeypatch this function directly to substitute a fake client rather
    than mutating the module-level singleton in place.
    """
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def reset_client_for_tests() -> None:
    """Drop the cached client — test-only seam (mirrors the reset helpers
    ``tests/conftest.py`` already calls for the JWKS/weather/checkpointer
    module-level singletons)."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Forced tool-use call
# ---------------------------------------------------------------------------


class AnthropicCallError(RuntimeError):
    """Raised when a single forced tool-use call fails: timeout, any
    Anthropic API error, or a response that (despite ``tool_choice`` forcing
    exactly one tool) carries no usable ``tool_use`` content block. Callers
    own the retry-once + degraded-mode policy — see module docstring."""


@dataclass(frozen=True)
class ToolCallResult:
    """Parsed result of one successful forced tool-use Anthropic call."""

    tool_input: dict[str, Any]
    tokens_in: int
    tokens_out: int
    model: str


async def call_tool_forced(
    *,
    system: str,
    user_content: str,
    tool: dict[str, Any],
    tool_name: str,
    max_tokens: int = 1024,
    timeout_seconds: float = CLASSIFICATION_BUDGET_SECONDS,
) -> ToolCallResult:
    """Make ONE Anthropic call with ``tool_choice`` forced to *tool_name*.
    Raises :class:`AnthropicCallError` on any failure. ``timeout_seconds``
    is a single attempt's timeout — callers compute it against a shared
    end-to-end deadline via :func:`new_deadline` / :func:`attempt_timeout`,
    never a flat per-attempt budget (see module docstring "Budget / retry").

    DETERMINISM NOTE (live-smoke finding, 2026-07-05): issue #32 specifies
    ``temperature=0``, but the ``temperature`` parameter is DEPRECATED on
    ``claude-sonnet-5`` — the API rejects requests that set it (400
    "`temperature` is deprecated for this model"). We therefore omit it;
    the determinism the spec intends is enforced by the eval discipline
    instead (eval-scenarios-v1: 3 samples per scenario, flaky = fail).

    ``tool`` is one of the ``app.agent.tools`` dicts (``{"name",
    "description", "input_schema"}``) — passed straight through as the sole
    entry of the ``tools`` list; ``tool_choice`` forces the model to call
    exactly that tool, so the response's ``content`` is expected to contain
    a single ``tool_use`` block whose ``input`` matches the tool's schema.
    """
    client = get_client()
    messages: list[MessageParam] = [cast("MessageParam", {"role": "user", "content": user_content})]
    tools: list[ToolParam] = [cast("ToolParam", tool)]
    tool_choice = cast("ToolChoiceToolParam", {"type": "tool", "name": tool_name})
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise AnthropicCallError(f"timed out after {timeout_seconds}s") from exc
    except anthropic.APIError as exc:
        raise AnthropicCallError(f"Anthropic API error: {type(exc).__name__}") from exc

    tool_use = next(
        (block for block in response.content if isinstance(block, ToolUseBlock)),
        None,
    )
    if tool_use is None:
        raise AnthropicCallError("no tool_use block in response despite forced tool_choice")

    usage = response.usage
    return ToolCallResult(
        tool_input=dict(tool_use.input),
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        model=response.model,
    )


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


def estimate_cost_cents(*, tokens_in: int, tokens_out: int) -> float:
    """Estimate the USD-cents cost of one call from its token usage.

    Conservative, hardcoded pricing — see module docstring "Cost
    accounting" and :data:`_PRICING_SOURCE_NOTE`. Rounded to 4 decimal
    places, matching the precision of the (deprecated, never-written)
    ``messages.llm_cost_cents numeric(10,4)`` column this figure would have
    populated — see ``app/agent/nodes/classify_severity.py``'s module
    docstring for why the canonical record is ``audit_log`` instead.
    """
    cost_usd = (tokens_in / 1_000_000) * _INPUT_PRICE_PER_MTOK_USD + (
        tokens_out / 1_000_000
    ) * _OUTPUT_PRICE_PER_MTOK_USD
    return round(cost_usd * 100, 4)


__all__: list[str] = [
    "CLASSIFICATION_BUDGET_SECONDS",
    "FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS",
    "MIN_RETRY_BUDGET_SECONDS",
    "MODEL",
    "AnthropicCallError",
    "ToolCallResult",
    "attempt_timeout",
    "call_tool_forced",
    "estimate_cost_cents",
    "get_client",
    "new_deadline",
    "reset_client_for_tests",
]
