"""Unit tests for app/integrations/anthropic.py (#31/#32/#33's shared call
site).

Marker: ``unit`` — the Anthropic SDK client is monkeypatched via
``get_client``; NO real network access, ever (never-run-real-API-calls
convention for this suite — a one-shot live smoke is run separately by the
orchestrator).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import ToolUseBlock

from app.integrations import anthropic as anthropic_mod

_TOOL: dict[str, Any] = {
    "name": "classify_severity",
    "description": "test tool",
    "input_schema": {"type": "object", "properties": {"severity": {"type": "string"}}},
}


def _fake_message(
    *,
    tool_name: str = "classify_severity",
    tool_input: dict[str, Any] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = "claude-sonnet-5",
    include_tool_use: bool = True,
) -> SimpleNamespace:
    content: list[Any] = []
    if include_tool_use:
        content.append(
            ToolUseBlock(
                id="toolu_test",
                input=tool_input or {"severity": "ROUTINE"},
                name=tool_name,
                type="tool_use",
            )
        )
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=content, usage=usage, model=model)


class _FakeMessages:
    def __init__(
        self,
        *,
        response: SimpleNamespace | None = None,
        exception: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._response = response
        self._exception = exception
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exception is not None:
            raise self._exception
        assert self._response is not None
        return self._response


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    anthropic_mod.reset_client_for_tests()
    yield
    anthropic_mod.reset_client_for_tests()


# ---------------------------------------------------------------------------
# get_client / reset_client_for_tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_client_returns_cached_singleton() -> None:
    client_a = anthropic_mod.get_client()
    client_b = anthropic_mod.get_client()
    assert client_a is client_b


@pytest.mark.unit
def test_reset_client_for_tests_drops_the_cache() -> None:
    client_a = anthropic_mod.get_client()
    anthropic_mod.reset_client_for_tests()
    client_b = anthropic_mod.get_client()
    assert client_a is not client_b


# ---------------------------------------------------------------------------
# call_tool_forced — success path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_tool_forced_parses_tool_use_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_messages = _FakeMessages(
        response=_fake_message(
            tool_input={"severity": "URGENT"}, input_tokens=123, output_tokens=45
        )
    )
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))

    result = await anthropic_mod.call_tool_forced(
        system="system prompt",
        user_content="tenant message",
        tool=_TOOL,
        tool_name="classify_severity",
    )

    assert result.tool_input == {"severity": "URGENT"}
    assert result.tokens_in == 123
    assert result.tokens_out == 45
    assert result.model == "claude-sonnet-5"

    # tool_choice forced -- the actual request payload. temperature is
    # deliberately ABSENT: deprecated on claude-sonnet-5 (400 if sent);
    # determinism is owned by the eval gate, not the sampling parameter.
    call_kwargs = fake_messages.calls[0]
    assert "temperature" not in call_kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "classify_severity"}
    assert call_kwargs["tools"] == [_TOOL]
    assert call_kwargs["system"] == "system prompt"
    assert call_kwargs["messages"] == [{"role": "user", "content": "tenant message"}]


# ---------------------------------------------------------------------------
# call_tool_forced — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_tool_forced_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_messages = _FakeMessages(response=_fake_message(), delay=0.2)
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))

    with pytest.raises(anthropic_mod.AnthropicCallError, match="timed out"):
        await anthropic_mod.call_tool_forced(
            system="s",
            user_content="u",
            tool=_TOOL,
            tool_name="classify_severity",
            timeout_seconds=0.01,
        )


@pytest.mark.unit
async def test_call_tool_forced_raises_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    api_error = anthropic.APIConnectionError(request=request)
    fake_messages = _FakeMessages(exception=api_error)
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))

    with pytest.raises(anthropic_mod.AnthropicCallError, match="Anthropic API error"):
        await anthropic_mod.call_tool_forced(
            system="s", user_content="u", tool=_TOOL, tool_name="classify_severity"
        )


@pytest.mark.unit
async def test_call_tool_forced_raises_when_no_tool_use_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_messages = _FakeMessages(response=_fake_message(include_tool_use=False))
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))

    with pytest.raises(anthropic_mod.AnthropicCallError, match="no tool_use block"):
        await anthropic_mod.call_tool_forced(
            system="s", user_content="u", tool=_TOOL, tool_name="classify_severity"
        )


# ---------------------------------------------------------------------------
# estimate_cost_cents
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_cost_cents_matches_pricing_formula() -> None:
    # 1,000,000 input tokens @ $3.00/MTok + 1,000,000 output tokens @ $15.00/MTok
    # = $18.00 = 1800 cents.
    cost = anthropic_mod.estimate_cost_cents(tokens_in=1_000_000, tokens_out=1_000_000)
    assert cost == pytest.approx(1800.0)


@pytest.mark.unit
def test_estimate_cost_cents_zero_tokens_is_zero() -> None:
    assert anthropic_mod.estimate_cost_cents(tokens_in=0, tokens_out=0) == 0.0


@pytest.mark.unit
def test_estimate_cost_cents_small_realistic_call() -> None:
    # 500 input / 150 output tokens -- realistic single classification call.
    cost = anthropic_mod.estimate_cost_cents(tokens_in=500, tokens_out=150)
    expected = round((500 / 1_000_000) * 3.00 * 100 + (150 / 1_000_000) * 15.00 * 100, 4)
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# new_deadline / attempt_timeout — the 20s END-TO-END budget split
# (spec-guardian ruling, 2026-07-05). Pure-function tests against a fake
# monotonic clock -- no real sleeps, so these run in microseconds and
# directly pin the numeric contract (12s cap / 2s floor / 20s total).
# ---------------------------------------------------------------------------


class _FakeClock:
    """A controllable stand-in for ``time.monotonic()`` — advance it
    explicitly to simulate elapsed time with zero real waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(anthropic_mod, "_now", clock.now)
    return clock


@pytest.mark.unit
def test_new_deadline_is_now_plus_budget(fake_clock: _FakeClock) -> None:
    deadline = anthropic_mod.new_deadline()
    assert deadline == pytest.approx(anthropic_mod.CLASSIFICATION_BUDGET_SECONDS)


@pytest.mark.unit
def test_first_attempt_capped_even_when_full_budget_remains(fake_clock: _FakeClock) -> None:
    deadline = anthropic_mod.new_deadline()  # 20s remaining
    timeout = anthropic_mod.attempt_timeout(deadline, is_retry=False)
    assert timeout == pytest.approx(anthropic_mod.FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS)  # 12s, not 20s


@pytest.mark.unit
def test_retry_gets_whatever_remains_after_first_attempt_cap(fake_clock: _FakeClock) -> None:
    deadline = anthropic_mod.new_deadline()  # deadline = t0 + 20s
    # Simulate the first attempt consuming its full 12s cap.
    fake_clock.advance(anthropic_mod.FIRST_ATTEMPT_TIMEOUT_CAP_SECONDS)
    timeout = anthropic_mod.attempt_timeout(deadline, is_retry=True)
    assert timeout == pytest.approx(8.0)  # 20 - 12 remaining


@pytest.mark.unit
def test_end_to_end_split_never_exceeds_the_shared_budget(fake_clock: _FakeClock) -> None:
    """The defining property: first-attempt allotment + retry allotment ==
    the ORIGINAL 20s budget, never 20s + 20s."""
    deadline = anthropic_mod.new_deadline()
    first = anthropic_mod.attempt_timeout(deadline, is_retry=False)
    assert first is not None
    fake_clock.advance(first)
    retry = anthropic_mod.attempt_timeout(deadline, is_retry=True)
    assert retry is not None
    assert first + retry == pytest.approx(anthropic_mod.CLASSIFICATION_BUDGET_SECONDS)


@pytest.mark.unit
def test_retry_skipped_when_remaining_budget_below_floor(fake_clock: _FakeClock) -> None:
    deadline = anthropic_mod.new_deadline()
    # Simulate the first attempt eating almost the whole 20s budget (e.g. it
    # took longer than its 12s cap to actually time out/return, plus retry
    # overhead), leaving less than the 2s floor.
    fake_clock.advance(anthropic_mod.CLASSIFICATION_BUDGET_SECONDS - 1.0)
    timeout = anthropic_mod.attempt_timeout(deadline, is_retry=True)
    assert timeout is None


@pytest.mark.unit
def test_retry_not_skipped_right_at_the_floor_boundary(fake_clock: _FakeClock) -> None:
    deadline = anthropic_mod.new_deadline()
    fake_clock.advance(
        anthropic_mod.CLASSIFICATION_BUDGET_SECONDS - anthropic_mod.MIN_RETRY_BUDGET_SECONDS
    )
    timeout = anthropic_mod.attempt_timeout(deadline, is_retry=True)
    assert timeout == pytest.approx(anthropic_mod.MIN_RETRY_BUDGET_SECONDS)


@pytest.mark.unit
def test_first_attempt_timeout_never_negative_past_deadline(fake_clock: _FakeClock) -> None:
    """Defensive: even if somehow called after the deadline has already
    passed, attempt_timeout never returns a negative timeout_seconds."""
    deadline = anthropic_mod.new_deadline()
    fake_clock.advance(anthropic_mod.CLASSIFICATION_BUDGET_SECONDS + 5.0)
    timeout = anthropic_mod.attempt_timeout(deadline, is_retry=False)
    assert timeout == 0.0
