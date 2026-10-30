# pocketpaw/tools/builtin/flow_tool.py — `start_flow` authoring tool
# (RFC 13 M3 → CHAIN FLOW v2).
#
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
# Updated: 2026-06-15 (feat/chain-flow-v2 — GENERALIZATION):
#   - The tool no longer caps the agent at "pick 1 of 2 hardcoded templates."
#     The `flow_type` ENUM + `required` flag are GONE. New params let the agent
#     author an ARBITRARY ephemeral mini-app as a FLAT step-graph:
#       `flow` (id), `entry` (first step id), `steps` (array of step objects),
#       optional `title`, optional `complete` (flow-level terminal default).
#   - `execute` routing: if `steps` is present → `build_flow_from_descriptor`
#     (the general path); elif `flow_type` is present → `build_flow` (a preset
#     shorthand the tool STILL accepts for the two known shapes). On a
#     `FlowBuildError`, the precise, agent-readable message is surfaced verbatim
#     so the model can fix the flat graph and retry (genesis forgiving-author
#     loop).
#   - The description is rewritten to teach the flat-graph shape (states, not
#     screens), mirroring the inline-prompt rule (`_inline.py`
#     `_MULTI_STEP_FLOW_RULE`).
#
# Updated: 2026-08-04 (fix/prompt-tells-the-truth — ONE CONTRACT, TWO SURFACES):
#   - `description` and `parameters` now return `_flows.START_FLOW_DESCRIPTION`
#     and `_flows.start_flow_parameters()`. They used to be hand-written copies
#     of the text in `agents/sdk_mcp_widgets.py`, 82.7% similar — and they had
#     already drifted. THIS copy, the one every runtime backend reads, was the
#     poorer of the two: it was missing the `set`-stepped anti-pattern, the
#     "terminal `complete` uses `action:`, never `type:`/`kind:`" rule, the
#     approve/reject-is-a-call_binding-button rule, and the repair-vs-reject
#     retry contract. Second split of this pair; the 2026-06-15 SPLIT-BRAIN FIX
#     was the first. The contract now lives beside the builder that enforces it.
#
# A `start_flow`-style builtin tool. The contract that makes it reliable: the
# agent NEVER hand-writes the recursively-nested chain/chain_map tree — it
# describes a FLAT graph (impossible to mis-nest), and a DETERMINISTIC builder
# (`pocketpaw.ripple._flows`) materializes + deep-validates the full nested tree
# and returns it as a `{version, ui}` inline-Ripple doc.
#
# The tool PRODUCES a `{version, ui}` JSON doc (a string the agent drops into a
# ```ui-spec fenced block — same envelope `_inline.py` mandates and the cloud
# extractor recognizes, RFC 13 M0). It does NOT import ripple TypeScript and
# does NOT mutate any pocket; the flow's state lives client-side in ripple's
# ChainExecutor once the doc renders.

from __future__ import annotations

import json
from typing import Any

from pocketpaw.ripple._flows import (
    FLOW_TYPES,
    START_FLOW_DESCRIPTION,
    FlowBuildError,
    build_flow,
    build_flow_from_descriptor,
    start_flow_parameters,
)
from pocketpaw.tools.protocol import BaseTool


class StartFlowTool(BaseTool):
    """Scaffold a complete multi-step Chain Flow / mini-app from a FLAT graph.

    The agent describes a flat step-graph (a `flow` id, an `entry` step id, and
    a `steps` array where each step points at the next by id string); a
    deterministic builder owns the nesting and DEEP-VALIDATES it, returning the
    whole tree as a ``{version, ui}`` doc the agent renders in a ``ui-spec``
    fence. A flat graph cannot mis-nest — that is the whole point.
    """

    @property
    def name(self) -> str:
        return "start_flow"

    @property
    def description(self) -> str:
        # Shared with the SDK MCP surface (``agents/sdk_mcp_widgets.py``). This
        # used to be a second hand-written copy that had already drifted: it was
        # missing the `set`-stepped anti-pattern, the "`action:` never
        # `type:`/`kind:`" rule, the approve/reject-is-a-call_binding-button
        # rule, and the repair-vs-reject retry contract. Every runtime backend
        # read the weaker text. See ``_flows.START_FLOW_DESCRIPTION``.
        return START_FLOW_DESCRIPTION

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        # Shared with the SDK MCP surface — see the description note above.
        # The schema was the second copy: same seven properties, written out
        # twice, with the ``flow_type`` enum read from FLOW_TYPES in both.
        return start_flow_parameters()

    async def execute(
        self,
        flow: str = "",
        entry: str = "",
        steps: list[dict[str, Any]] | str | None = None,
        title: str | None = None,
        complete: dict[str, Any] | str | None = None,
        flow_type: str = "",
        domain: str | None = None,
        config: dict[str, Any] | str | None = None,
        **kwargs: Any,
    ) -> str:
        """Materialize a flat descriptor (or a preset) into a `{version, ui}`
        flow doc (JSON string)."""
        # Subprocess / CLI callers can't pass nested objects through a flat
        # string signature — coerce JSON-string args back to structures.
        steps = self._coerce_json(steps, "steps")
        if isinstance(steps, str):  # coercion failed → it returned an Error
            return steps
        complete = self._coerce_json(complete, "complete", allow_dict=True)
        if isinstance(complete, str) and complete.startswith("Error:"):
            return complete
        config = self._coerce_json(config, "config", allow_dict=True)
        if isinstance(config, str) and config.startswith("Error:"):
            return config

        # Route: a flat `steps` graph takes precedence; else a preset shorthand.
        if steps is not None:
            if not isinstance(steps, list):
                return self._error("`steps` must be an array of step objects.")
            descriptor: dict[str, Any] = {
                "flow": flow or "flow",
                "entry": entry,
                "steps": steps,
            }
            if title:
                descriptor["title"] = title
            if complete is not None:
                descriptor["complete"] = complete
            try:
                doc = build_flow_from_descriptor(descriptor)
            except FlowBuildError as exc:
                # Precise, agent-readable: the model fixes the flat graph and
                # retries rather than getting a stack trace.
                return self._error(str(exc))
            return self._success(self._dump(doc))

        if flow_type:
            if config is not None and not isinstance(config, dict):
                return self._error("`config` must be an object (key/value map).")
            try:
                doc = build_flow(flow_type, domain=domain, config=config)
            except (FlowBuildError, ValueError) as exc:
                return self._error(str(exc))
            return self._success(self._dump(doc))

        valid = ", ".join(sorted(FLOW_TYPES))
        return self._error(
            "start_flow needs either a flat `steps` graph (with `flow` and "
            "`entry`) or a `flow_type` preset shorthand "
            f"(one of: {valid})."
        )

    @staticmethod
    def _coerce_json(value: Any, name: str, *, allow_dict: bool = False) -> Any:
        """Coerce a JSON-string arg into a structure; pass through if already
        one. Returns an ``Error: …`` string on bad JSON so ``execute`` can
        relay it."""
        if value is None or not isinstance(value, str):
            return value
        s = value.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return f"Error: `{name}` was a string but not valid JSON."

    @staticmethod
    def _dump(doc: dict[str, Any]) -> str:
        """Serialize the doc, dropping the internal `_warnings` key (soft
        single-branch-reachability notes are for build-time guidance, not part
        of the rendered spec)."""
        clean = {k: v for k, v in doc.items() if k != "_warnings"}
        return json.dumps(clean, ensure_ascii=False)


__all__ = ["StartFlowTool"]
