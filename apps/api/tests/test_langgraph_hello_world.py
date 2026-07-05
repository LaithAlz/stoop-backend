"""#26 acceptance criterion (deferred-with-credential): "hello-world graph
run appears as a trace in LangSmith."

There is no LangSmith account yet (see ``app/config.py``'s
``langsmith_api_key`` docstring) — that specific AC cannot be verified
until one exists; a human must create the account and set
``LANGSMITH_API_KEY``/``LANGSMITH_PROJECT`` before it can be checked for
real (see ``apps/api/CLAUDE.md``: "Things humans must do ... LangSmith
account creation").

What CAN be verified today, without any network access or credentials:
1. A trivial LangGraph ``StateGraph`` compiles and runs locally
   (``langgraph`` is correctly installed/importable and its basic API
   works end to end).
2. The env-var wiring that would light tracing up automatically the day a
   LangSmith key exists (``app/observability.py``'s
   ``init_langsmith_tracing()`` — exercised in ``tests/test_observability.py``,
   not duplicated here).

Once a LangSmith account exists, re-running this exact graph with
``LANGSMITH_API_KEY``/``LANGSMITH_PROJECT`` set (and
``init_langsmith_tracing()`` called, as it is at every app startup) is
what will make a trace appear — closing the deferred AC without any code
change.
"""

from __future__ import annotations

from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph


class _HelloState(TypedDict):
    """Minimal state — a single string channel."""

    greeting: str


def _say_hello(state: _HelloState) -> _HelloState:
    return {"greeting": f"hello, {state['greeting']}"}


def _build_hello_world_graph() -> object:
    graph: StateGraph[_HelloState, None, _HelloState, _HelloState] = StateGraph(_HelloState)
    graph.add_node("say_hello", _say_hello)
    graph.set_entry_point("say_hello")
    graph.add_edge("say_hello", END)
    return graph.compile()


@pytest.mark.unit
def test_hello_world_graph_compiles() -> None:
    """A trivial StateGraph compiles without touching the network."""
    compiled = _build_hello_world_graph()
    assert compiled is not None


@pytest.mark.unit
async def test_hello_world_graph_runs_locally() -> None:
    """The compiled graph actually runs end to end, entirely locally."""
    compiled = _build_hello_world_graph()

    result = await compiled.ainvoke({"greeting": "world"})  # type: ignore[attr-defined]

    assert result["greeting"] == "hello, world"
