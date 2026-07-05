"""LangGraph node implementations — `docs/03-engineering/architecture.md`
§4/§5's ``agent/nodes/`` package.

Each module exposes one ``async def <node_name>(state: AgentState) ->
dict[str, Any]`` function, matching LangGraph's node signature (a partial
state update, merged into the running state by the graph). #34 ("wire state
graph with Postgres checkpointer") assembles these into an actual
``StateGraph`` — that issue owns ``app/agent/graph.py``; these modules are
independently importable/testable without it.
"""

from __future__ import annotations
