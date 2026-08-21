# pocketpaw/tools/builtin/studio_flow_tool.py — `build_studio_flow` authoring tool
# (studio /flow agent building).
#
# Created: 2026-08-21 (feat/studio-real-backend, agent-drawn studio flows).
#
# The /studio Flow canvas is a node graph (SvelteFlow) that the user RUNS —
# generation happens only when they click "Run all". This tool lets the agent
# BUILD that graph from a natural-language goal: the model calls
# ``build_studio_flow`` with a validated node/edge spec, the tool normalises +
# validates it, and the loop fans a dedicated ``studio_flow`` SystemEvent to
# the chat SSE stream so paw-enterprise can materialise the nodes on the
# canvas live.
#
# The tool PRODUCES ``{"studio_flow": {"goal", "nodes", "edges"}}`` — the exact
# marker envelope ``agents/loop.py::_publish_studio_flow_event`` recognises
# (mirroring the pocket_event pattern). It does NOT generate media and does NOT
# run the pipeline; the canvas stays idle until "Run all".
#
# Two surfaces read the SAME contract so they cannot drift (the SPLIT-BRAIN
# lesson from ``flow_tool.py``):
#   • the runtime BaseTool (this file) — reached by the function-calling
#     backends (openai_agents / google_adk / deep_agents / pydantic_ai) via
#     ``tools/builtin/_LAZY_IMPORTS``;
#   • the in-process SDK MCP server ``agents/sdk_mcp_studio.py`` — reached by
#     the default ``claude_agent_sdk`` backend, exposing the same
#     ``build_studio_flow`` tool with the same description / schema / validator.

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# The node kinds the /studio Flow canvas knows. Keep in sync with the
# frontend ``FlowNodeKind`` (src/lib/core/studio/types.ts). The agent must
# never invent a kind — the validator rejects unknown types.
STUDIO_FLOW_KINDS: tuple[str, ...] = (
    "text",
    "image",
    "video",
    "output",
    "selector",
    "picture",
    "model",
    "toolcall",
)

# Node kinds a text/prompt flows through vs. structural-only. Purely advisory
# for the description; the validator accepts any known kind.
_PROMPT_KINDS = ("text", "image", "video")

STUDIO_FLOW_DESCRIPTION = (
    "Build a node graph on the /studio Flow canvas from a natural-language "
    "goal. This ONLY scaffolds the blocks and their connections — it does NOT "
    "generate any image or video. Generation happens later, when the user "
    "clicks the canvas's 'Run all' button.\n\n"
    "The canvas is a pipeline of these node kinds (their ids are your choice, "
    "but must be unique and non-empty):\n"
    "  model   — chooses the text model (data.textModel) + the image "
    "generation model (data.imageModel) the pipeline inherits. Put one FIRST.\n"
    "  text    — the idea/prompt block. data.text holds the raw goal sentence; "
    "data.prompt (optional) holds the enriched cinematic prompt you wrote for "
    "the media.\n"
    "  image / video — the media node that generates. data.prompt is what runs; "
    "data.aspectRatio is one of '1:1','16:9','9:16','4:3','3:4','3:2','2:3'; "
    "data.styleId 'none' or a style id; data.durationSec (video only).\n"
    "  picture — a reference/input image (data.pictureUrl). Optional; wire it "
    "when the goal implies an input asset (e.g. 'make a cinematic poster from "
    "this image').\n"
    "  toolcall — post-processing applied on run: data.toolRatio (aspect the "
    "downstream media inherits), data.toolUpscale (bool), data.toolRemoveBg "
    "(bool), data.toolLayers (int, 0 = off). Wire it after the media node.\n"
    "  output  — the final terminal node.\n\n"
    "Canonical wiring for a cinematic-photo goal: model → text (with the "
    "enriched prompt) → image → [toolcall] → output. For an input-image goal: "
    "model → picture → text → image → toolcall → output. Add a video node "
    "instead of image when the goal is a clip.\n\n"
    "Provide ``nodes`` as an array of {id, type, position:{x,y}, data:{}} and "
    "``edges`` as an array of {source, target} referencing node ids. Positions "
    "spread the graph left→right (x steps ~320). The tool validates the graph "
    "and returns the canonical spec.\n\n"
    "When the session carries a Studio Flow context (ACTIVE FLOW ID), pass "
    "``flow_id`` EXACTLY as given — the graph then persists into that flow "
    "project instead of a new one. Never invent a flow_id."
)


def studio_flow_parameters() -> dict[str, Any]:
    """JSON Schema for ``build_studio_flow`` — shared by the BaseTool and the
    in-process SDK MCP server so both advertise the identical contract."""
    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "The user's original goal the flow was built from (used as "
                    "context/label, not executed)."
                ),
            },
            "flow_id": {
                "type": "string",
                "description": (
                    "The flow project this graph belongs to. Required when the "
                    "session carries an ACTIVE FLOW ID — pass it exactly, so the "
                    "flow saves into that project. Omit when there is no flow "
                    "context."
                ),
            },
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "enum": list(STUDIO_FLOW_KINDS)},
                        "position": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                            },
                        },
                        "data": {"type": "object"},
                    },
                    "required": ["id", "type"],
                },
                "description": (
                    "The blocks of the pipeline. Each must carry a unique id, "
                    "a valid type, a position, and a data object."
                ),
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                    },
                    "required": ["source", "target"],
                },
                "description": (
                    "Connections between nodes (optional). Each source/target "
                    "must reference a node id from ``nodes``."
                ),
            },
        },
        "required": ["nodes"],
    }


def validate_flow_spec(
    nodes: Any,
    edges: Any | None = None,
    goal: str = "",
    flow_id: str = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate + normalise a flow spec. Returns ``(spec, error)`` — one of
    them is None. Shared by the BaseTool and the SDK MCP handler so both
    surfaces enforce the identical graph rules. ``flow_id`` is echoed back
    verbatim (when non-empty) so the loop / frontend know which flow this
    graph belongs to."""
    if not isinstance(nodes, list) or not nodes:
        return None, "`nodes` must be a non-empty array of node objects."

    seen: set[str] = set()
    clean_nodes: list[dict[str, Any]] = []
    for i, raw in enumerate(nodes):
        if not isinstance(raw, dict):
            return None, f"nodes[{i}] must be an object."
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            return None, f"nodes[{i}] needs a non-empty string `id`."
        if node_id in seen:
            return None, f"duplicate node id {node_id!r}."
        seen.add(node_id)
        ntype = raw.get("type")
        if ntype not in STUDIO_FLOW_KINDS:
            return None, (
                f"nodes[{i}] type {ntype!r} is invalid — must be one of: "
                + ", ".join(STUDIO_FLOW_KINDS)
                + "."
            )
        pos = raw.get("position") or {}
        if not isinstance(pos, dict):
            pos = {}
        try:
            px = float(pos.get("x", 40 + i * 40))
            py = float(pos.get("y", 60 + i * 40))
        except (TypeError, ValueError):
            px, py = 40.0 + i * 40, 60.0 + i * 40
        data = raw.get("data")
        if not isinstance(data, dict):
            data = {}
        clean_nodes.append(
            {
                "id": node_id,
                "type": ntype,
                "position": {"x": px, "y": py},
                "data": data,
            }
        )

    clean_edges: list[dict[str, Any]] = []
    if edges is None:
        edges = []
    if not isinstance(edges, list):
        return None, "`edges` must be an array of {source, target}."
    for i, raw in enumerate(edges):
        if not isinstance(raw, dict):
            return None, f"edges[{i}] must be an object."
        source = raw.get("source")
        target = raw.get("target")
        if source not in seen:
            return None, f"edges[{i}] source {source!r} does not match any node id."
        if target not in seen:
            return None, f"edges[{i}] target {target!r} does not match any node id."
        clean_edges.append({"source": source, "target": target})

    spec: dict[str, Any] = {
        "goal": goal or "",
        "nodes": clean_nodes,
        "edges": clean_edges,
    }
    if flow_id:
        spec["flow_id"] = flow_id
    return (spec, None)


def dump_flow_spec(spec: dict[str, Any]) -> str:
    """Serialize the validated spec in the ``{"studio_flow": ...}`` marker
    envelope the loop recognises."""
    return json.dumps({"studio_flow": spec}, ensure_ascii=False)


def persist_flow_spec(spec: dict[str, Any]) -> bool:
    """Persist an agent-built flow graph into the flow-projects store.

    Called from BOTH surfaces (``StudioFlowTool.execute`` and the SDK MCP
    handler) so a ``build_studio_flow`` call saves the graph server-side the
    moment the agent produces it — independent of the SSE transport that
    materialises the canvas. This is the fix for "agent-built flows never
    reach the backend": the old design relied on the frontend re-PUTting
    after the canvas rendered, which silently dropped the build whenever the
    SSE event didn't arrive. The store is the SAME upsert the frontend's
    ``PUT /studio/flow-projects/{id}`` uses, so a manual save and an
    agent save never diverge.

    Workspace is resolved from the per-stream identity ContextVar
    (``run_core.attach_agent_identity``), the exact seam the pocket
    specialist MCP tool uses — so the record lands under the SAME
    workspace the /studio flow-projects router reads. Returns False (never
    raises) when there is nothing to save or the EE store isn't reachable —
    a save miss must never fail the tool call.
    """
    flow_id = str(spec.get("flow_id") or "").strip()
    nodes = spec.get("nodes") or []
    if not flow_id or not isinstance(nodes, list) or not nodes:
        return False
    edges = spec.get("edges") or []
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id
        from pocketpaw_ee.cloud.studio.schemas import FlowEdge, FlowNode
        from pocketpaw_ee.cloud.studio.service import save_flow_project

        workspace_id = current_workspace_id()
        if not workspace_id:
            logger.debug(
                "studio: no workspace identity bound — skipping server-side "
                "flow persist (flow_id=%s)",
                flow_id,
            )
            return False
        # The validator's edges carry only {source, target}; the store's
        # FlowEdge requires an id — synthesize a stable one when absent.
        typed_edges = [
            FlowEdge(
                id=str(e.get("id") or f"e_{e.get('source', '')}_{e.get('target', '')}"),
                source=str(e.get("source", "")),
                target=str(e.get("target", "")),
                sourceHandle=e.get("sourceHandle"),
                targetHandle=e.get("targetHandle"),
            )
            for e in edges
            if isinstance(e, dict)
        ]
        typed_nodes = [FlowNode.model_validate(n) for n in nodes if isinstance(n, dict)]
        save_flow_project(
            flow_id,
            workspace_id,
            # Keep the existing title; the store falls back to "Flow" for a
            # brand-new project rather than stamping the raw goal as a name.
            name=None,
            nodes=typed_nodes,
            edges=typed_edges,
        )
        logger.info(
            "Studio flow persisted server-side: flow_id=%s workspace=%s nodes=%d edges=%d",
            flow_id,
            workspace_id,
            len(typed_nodes),
            len(typed_edges),
        )
        return True
    except Exception:  # noqa: BLE001 — persistence must never fail the tool call
        logger.warning("studio: server-side flow persist failed (non-fatal)", exc_info=True)
        return False


class StudioFlowTool(BaseTool):
    """Scaffold a /studio Flow node graph from a natural-language goal.

    Returns a validated ``{"studio_flow": {goal, nodes, edges}}`` spec the
    frontend materialises on the canvas. Deliberately does NOT generate — the
    pipeline runs only when the user clicks "Run all".
    """

    @property
    def name(self) -> str:
        return "build_studio_flow"

    @property
    def description(self) -> str:
        return STUDIO_FLOW_DESCRIPTION

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return studio_flow_parameters()

    async def execute(
        self,
        nodes: Any,
        edges: Any | None = None,
        goal: str = "",
        flow_id: str = "",
        **kwargs: Any,
    ) -> str:
        """Validate the node/edge graph and return the canonical flow spec.

        ``flow_id`` (when the agent passes it) is echoed verbatim so the loop
        and the frontend know exactly which flow project this graph belongs to.
        """
        if isinstance(nodes, str):
            try:
                nodes = json.loads(nodes)
            except json.JSONDecodeError:
                return self._error("`nodes` was a string but not valid JSON.")
        if isinstance(edges, str):
            try:
                edges = json.loads(edges)
            except json.JSONDecodeError:
                return self._error("`edges` was a string but not valid JSON.")

        spec, error = validate_flow_spec(nodes, edges, goal, flow_id or "")
        if error is not None:
            # Precise, agent-readable: the model fixes the graph and retries
            # rather than getting a stack trace.
            return self._error(error)
        # Save the graph to the flow-projects store the moment the agent
        # produces it — never depend on the SSE/ frontend re-PUT to persist.
        persist_flow_spec(spec)
        return self._success(dump_flow_spec(spec))


__all__ = [
    "StudioFlowTool",
    "STUDIO_FLOW_DESCRIPTION",
    "STUDIO_FLOW_KINDS",
    "studio_flow_parameters",
    "validate_flow_spec",
    "dump_flow_spec",
    "persist_flow_spec",
]
