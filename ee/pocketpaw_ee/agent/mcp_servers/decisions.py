# decisions.py — In-process MCP server exposing the cloud decision graph.
# Created: 2026-05-25 (RFC 07 Slice 2) — wraps the in-process
#   `DecisionGraph` Python API so MCP-capable agent backends (currently
#   `claude_agent_sdk`) can pull audit / trace / explainability context
#   without going through the HTTP surface. Mirrors the pattern in
#   `tasks.py` and `planner.py`: agent identity comes from the per-stream
#   ContextVars in `ee.cloud.chat.agent_service`; outside an SSE stream
#   the tools return a clear MCP error rather than silently mis-tenanting.
# Updated: 2026-05-25 (RFC 07 Slice 3a) — added `decisions_explain`,
#   the natural-language Q&A wrapper. Same identity + scope contract
#   as the read tools; delegates to the cloud-side orchestrator at
#   `pocketpaw_ee.cloud.decisions.explain.explain`.
#
# Tools registered:
#   - decisions_get(decision_id)
#   - decisions_find(actor=, since=, until=, scope_kind=, pocket_id=,
#                    policy=, outcome_status=, limit=)
#   - decisions_trace(decision_id, depth=3)
#   - decisions_explain(question, scope=, max_decisions=, depth=,
#                       backend=)
#
# The wire shape mirrors the REST router so REST + MCP never drift.
"""Agent-side MCP surface for the decision-graph entity.

These tools let an agent ask "what did we decide, why, and what came
of it" without leaving its loop. The wrappers thin-shim the in-process
`DecisionGraph` Python API so the SDK never owns a copy of the wire
contract — change the DTO once and both REST + MCP track it.

The scope filter is the load-bearing audit invariant: every call passes
`requester_scopes=[f"workspace:{workspace_id}"]` (resolved from the
chat stream's per-request ContextVars). A Decision outside that scope
returns the same "not found" envelope as a Decision that truly doesn't
exist — agents cannot probe for hidden rows via the MCP surface, just
as they can't via REST.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_decisions"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
DECISIONS_GET_TOOL_ID = f"mcp__{SERVER_NAME}__decisions_get"
DECISIONS_FIND_TOOL_ID = f"mcp__{SERVER_NAME}__decisions_find"
DECISIONS_TRACE_TOOL_ID = f"mcp__{SERVER_NAME}__decisions_trace"
DECISIONS_EXPLAIN_TOOL_ID = f"mcp__{SERVER_NAME}__decisions_explain"

DECISIONS_TOOL_IDS = (
    DECISIONS_GET_TOOL_ID,
    DECISIONS_FIND_TOOL_ID,
    DECISIONS_TRACE_TOOL_ID,
    DECISIONS_EXPLAIN_TOOL_ID,
)


# ---------------------------------------------------------------------------
# MCP response envelopes — same shape as the tasks / planner servers
# ---------------------------------------------------------------------------


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying `body` as JSON text."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Identity resolution — same chokepoint the tasks / planner servers use
# ---------------------------------------------------------------------------


def _identity() -> tuple[str | None, str | None]:
    """Resolve workspace + agent-user-id from per-stream ContextVars.

    Returns `(workspace_id, user_id)` — the agent's runtime authenticates
    as itself when calling into the cloud, so `user_id` IS the agent
    identity from the auth layer's perspective. Both values come back
    `None` when the tool is invoked outside an SSE chat stream; the
    handler returns a clear MCP error rather than silently mis-tenanting.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:
        return None, None


def _requester_scopes(workspace_id: str) -> list[str]:
    """Build the scope-tag list for a workspace-scoped requester.

    Same contract the REST router enforces — Decisions are visible to a
    requester whose scope list intersects with the Decision's scope
    tags. A workspace caller can see decisions tagged
    `workspace:<id>` (and anything that shares one of those tags).
    """
    return [f"workspace:{workspace_id}"]


# ---------------------------------------------------------------------------
# Wire helpers — share the REST router's wire shape via dto.DecisionResponse
# ---------------------------------------------------------------------------


def _decision_wire(decision: Any) -> dict[str, Any]:
    """Map a domain `Decision` to the wire dict the REST surface returns."""
    from pocketpaw_ee.cloud.decisions.dto import DecisionResponse

    return DecisionResponse.from_domain(decision).model_dump(mode="json")


def _trace_wire(result: Any) -> dict[str, Any]:
    """Map a service-layer `TraceResult` to the wire dict."""
    from pocketpaw_ee.cloud.decisions.dto import (
        DecisionResponse,
        DecisionTraceResponse,
        EdgeDTO,
        TraceNodeResponse,
    )

    wire = DecisionTraceResponse(
        root=result.root,
        nodes={
            node_id: TraceNodeResponse(
                id=node.id,
                kind=node.kind,
                decision=DecisionResponse.from_domain(node.decision) if node.decision else None,
                label=node.label,
            )
            for node_id, node in result.nodes.items()
        },
        edges=[
            EdgeDTO(
                src=str(e.src_id),
                target=e.target_id,
                relation=e.relation,
                weight=e.weight,
            )
            for e in result.edges
        ],
        truncated=result.truncated,
        truncated_count=result.truncated_count,
        depth_reached=result.depth_reached,
    )
    return wire.model_dump(mode="json")


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a datetime; tolerate None / bad input."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _decisions_get_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — decisions_get can only be called "
            "from inside a cloud SSE chat stream"
        )

    decision_id_raw = args.get("decision_id")
    if not isinstance(decision_id_raw, str) or not decision_id_raw:
        return _error_response("decision_id is required (string UUID)")
    try:
        decision_id = UUID(decision_id_raw)
    except (ValueError, TypeError):
        return _error_response(f"'{decision_id_raw}' is not a valid UUID")

    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    try:
        graph = get_decision_graph()
        decision = await graph.get(
            decision_id,
            requester_scopes=_requester_scopes(workspace_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_get failed", exc_info=True)
        return _error_response(f"decisions_get failed: {exc}")

    if decision is None:
        # Same envelope as a real miss — agents cannot probe for hidden rows.
        return _success_response({"decision": None, "found": False})
    return _success_response({"decision": _decision_wire(decision), "found": True})


async def _decisions_find_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — decisions_find can only be called "
            "from inside a cloud SSE chat stream"
        )

    actor = args.get("actor") or None
    pocket_id = args.get("pocket_id") or None
    policy = args.get("policy") or None
    outcome_status = args.get("outcome_status") or None
    scope_kind = args.get("scope_kind") or None
    input_id = args.get("input_id") or None
    since = _parse_iso(args.get("since"))
    until = _parse_iso(args.get("until"))
    limit_raw = args.get("limit") or 50
    try:
        limit = max(1, min(int(limit_raw), 200))
    except (TypeError, ValueError):
        limit = 50

    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    try:
        graph = get_decision_graph()
        decisions = await graph.find(
            actor=actor,
            since=since,
            until=until,
            scope_kind=scope_kind,
            pocket_id=pocket_id,
            policy=policy,
            outcome_status=outcome_status,
            input_id=input_id,
            limit=limit,
            requester_scopes=_requester_scopes(workspace_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_find failed", exc_info=True)
        return _error_response(f"decisions_find failed: {exc}")

    return _success_response(
        {
            "decisions": [_decision_wire(d) for d in decisions],
            "count": len(decisions),
        }
    )


async def _decisions_trace_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — decisions_trace can only be called "
            "from inside a cloud SSE chat stream"
        )

    decision_id_raw = args.get("decision_id")
    if not isinstance(decision_id_raw, str) or not decision_id_raw:
        return _error_response("decision_id is required (string UUID)")
    try:
        decision_id = UUID(decision_id_raw)
    except (ValueError, TypeError):
        return _error_response(f"'{decision_id_raw}' is not a valid UUID")

    depth_raw = args.get("depth") or 3
    try:
        depth = max(1, min(int(depth_raw), 10))
    except (TypeError, ValueError):
        depth = 3

    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    try:
        graph = get_decision_graph()
        result = await graph.trace(
            decision_id,
            depth=depth,
            requester_scopes=_requester_scopes(workspace_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_trace failed", exc_info=True)
        return _error_response(f"decisions_trace failed: {exc}")

    return _success_response(_trace_wire(result))


async def _decisions_explain_handler(args: dict) -> dict:
    """Wrap the cloud-side explain orchestrator for MCP callers.

    Same identity / scope contract as the read tools — the per-stream
    workspace + agent id are pulled from ContextVars; outside an SSE
    stream the handler returns an MCP error.
    """
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — decisions_explain can only be called "
            "from inside a cloud SSE chat stream"
        )

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return _error_response("question is required (non-empty string)")

    scope = args.get("scope") if isinstance(args.get("scope"), dict) else None
    max_decisions_raw = args.get("max_decisions") or 5
    try:
        max_decisions = max(1, min(int(max_decisions_raw), 20))
    except (TypeError, ValueError):
        max_decisions = 5

    depth_raw = args.get("depth") or 3
    try:
        depth = max(1, min(int(depth_raw), 10))
    except (TypeError, ValueError):
        depth = 3

    backend = args.get("backend")
    if backend not in {"llm", "templated", None}:
        backend = None

    from pocketpaw_ee.cloud.decisions.dto import ExplanationResponse
    from pocketpaw_ee.cloud.decisions.explain import ExplainRequestInput, explain

    body = ExplainRequestInput(
        question=question.strip(),
        scope=scope,
        max_decisions=max_decisions,
        depth=depth,
        backend=backend,
    )

    try:
        explanation = await explain(
            body,
            requester_scopes=_requester_scopes(workspace_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("decisions_explain failed", exc_info=True)
        return _error_response(f"decisions_explain failed: {exc}")

    wire = ExplanationResponse.from_domain(explanation)
    return _success_response(wire.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Server factory — matches the shape of `build_tasks_context_server`
# ---------------------------------------------------------------------------


def build_decisions_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the decision graph, or
    return `None` if the Claude Agent SDK isn't installed.

    Returned shape matches the other in-process servers (tasks, planner,
    pockets) so the backend's MCP registration loop in `claude_sdk.py`
    treats every server identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_decisions MCP disabled")
        return None

    @tool(
        "decisions_get",
        (
            "Look up one Decision from the decision graph by its UUID. "
            "Returns the full Decision record — actor, intent, action, "
            "inputs, approvers, outcome, precedents, hash_link — when the "
            "Decision exists AND is visible in the caller's workspace "
            "scope. Returns `{found: false, decision: null}` when the "
            "Decision is unknown OR outside the caller's scope (the two "
            "states are deliberately indistinguishable — agents cannot "
            "probe for hidden rows). Use this when the user asks 'why did "
            "we decide X?' and you already have the decision id from a "
            "trace, downstream walk, or list result."
        ),
        {"decision_id": str},
    )
    async def decisions_get(args):  # type: ignore[no-untyped-def]
        return await _decisions_get_handler(args)

    @tool(
        "decisions_find",
        (
            "Multi-axis filter over recent decisions. Every filter is "
            "optional; combine to narrow. Common shapes: "
            "`{actor: 'did:soul:agent1'}` (everything one agent decided), "
            "`{pocket_id: 'p_renewals', since: '2026-05-01T00:00:00Z'}` "
            "(everything one pocket decided this month), "
            "`{outcome_status: 'rejected'}` (every refused decision in the "
            "workspace). Returns up to `limit` Decisions (default 50, max "
            "200) sorted newest-first. Scope-filtered — only Decisions in "
            "the caller's workspace scope are returned. Use this to build "
            "the audit + 'similar past calls' surface."
        ),
        {
            "actor": str,
            "since": str,
            "until": str,
            "scope_kind": str,
            "pocket_id": str,
            "policy": str,
            "outcome_status": str,
            "input_id": str,
            "limit": int,
        },
    )
    async def decisions_find(args):  # type: ignore[no-untyped-def]
        return await _decisions_find_handler(args)

    @tool(
        "decisions_trace",
        (
            "Walk the decision graph upstream from one Decision. Returns "
            "a depth-bounded BFS over `precedent` and `input` edges — the "
            "facts and prior calls that fed this Decision. `approval` and "
            "`outcome` edges are surfaced as terminal labels (not walked "
            "further) so the trace stays narratable. Defaults: depth=3, "
            "max_fanout=20 per node. Use this to answer 'what did we "
            "consider when we made this call?' — the response carries "
            "every Decision + InputRef in the upstream subgraph plus the "
            "edges that link them. Scope-filtered: nodes outside the "
            "caller's workspace scope are silently elided."
        ),
        {"decision_id": str, "depth": int},
    )
    async def decisions_trace(args):  # type: ignore[no-untyped-def]
        return await _decisions_trace_handler(args)

    @tool(
        "decisions_explain",
        (
            "Ask a natural-language question of the decision graph and get "
            "a grounded narrative answer with citations. The pipeline "
            "extracts entities from the question (Haiku call, cached), "
            "finds candidate decisions in the caller's scope, walks a "
            "depth-bounded trace upstream from the top candidate, and "
            "narrates the result with Sonnet (or a deterministic "
            'templated narrator when `backend="templated"` is set or '
            "the LLM call fails). Every sentence in the narrative cites "
            "a decision id; the response also surfaces "
            "`decisions_walked` (every node in the trace) and "
            "`ungrounded_sentences` (anything the verifier stripped). "
            "Use this to answer free-form 'why did we...' questions when "
            "you don't already have a decision id — for known ids, "
            "prefer `decisions_get` + `decisions_trace`."
        ),
        {
            "question": str,
            "scope": dict,
            "max_decisions": int,
            "depth": int,
            "backend": str,
        },
    )
    async def decisions_explain(args):  # type: ignore[no-untyped-def]
        return await _decisions_explain_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[decisions_get, decisions_find, decisions_trace, decisions_explain],
    )
    return SERVER_NAME, server


__all__ = [
    "DECISIONS_EXPLAIN_TOOL_ID",
    "DECISIONS_FIND_TOOL_ID",
    "DECISIONS_GET_TOOL_ID",
    "DECISIONS_TOOL_IDS",
    "DECISIONS_TRACE_TOOL_ID",
    "SERVER_NAME",
    "build_decisions_context_server",
]
