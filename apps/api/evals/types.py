"""Shared type aliases for the eval harness."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.integrations import anthropic as anthropic_mod

ToolCaller = Callable[..., Awaitable[anthropic_mod.ToolCallResult]]
"""Matches ``app.integrations.anthropic.call_tool_forced``'s keyword-only
signature exactly: ``(*, system, user_content, tool, tool_name, max_tokens,
timeout_seconds) -> ToolCallResult`` (a coroutine). Every real Anthropic
call this harness makes (classification samples, the draft call, the judge
call) goes through a value of this type -- the REAL transport function in
the paid gate, or a scenario-aware stub with the identical signature under
``EVAL_DRY_RUN=1``. This is the one seam the whole dry-run design rests on:
see ``evals/runner.py``'s module docstring."""

ToolDict = dict[str, Any]

__all__: list[str] = ["ToolCaller", "ToolDict"]
