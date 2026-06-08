# pocketpaw/tools/builtin/flow_tool.py — `start_flow` authoring tool (RFC 13 M3).
#
# Created: 2026-05-31 (RFC 13 M3, feat/m3-flow-authoring-tool).
#
# A `start_flow`-style builtin tool. The contract that makes it reliable:
#   - the LLM emits a TINY descriptor — `flow_type` (an enum of known
#     templates), an optional `domain` hint, optional `config` overrides;
#   - a DETERMINISTIC builder (`pocketpaw.ripple._flows`) expands a hardcoded
#     template into the full nested chain/chain_map Chain Flow tree and returns
#     it as a `{version, ui}` inline-Ripple doc.
#
# The model never hand-writes the recursively-nested tree — the genesis
# `preprocessChainStrings` repair code (RFC 13 §2.2) is the evidence that
# hand-authoring is fragile. The model picks a template; Python owns the tree.
#
# The tool PRODUCES a `{version, ui}` JSON doc (a string the agent drops into a
# ```ui-spec fenced block — same envelope `_inline.py` mandates and the cloud
# extractor recognizes, RFC 13 M0). It does NOT import ripple TypeScript and
# does NOT mutate any pocket; the flow's state lives client-side in ripple's
# ChainExecutor once the doc renders.

from __future__ import annotations

import json
from typing import Any

from pocketpaw.ripple._flows import FLOW_TYPES, build_flow
from pocketpaw.tools.protocol import BaseTool


class StartFlowTool(BaseTool):
    """Scaffold a complete multi-step Chain Flow from a tiny descriptor.

    The agent calls this instead of hand-authoring a nested chain/chain_map
    tree. It emits ~3 fields; a deterministic builder returns the whole tree
    as a ``{version, ui}`` doc the agent then renders in a ``ui-spec`` fence.
    """

    @property
    def name(self) -> str:
        return "start_flow"

    @property
    def description(self) -> str:
        return (
            "Scaffold a complete multi-step flow (wizard / intake / survey) "
            "from a tiny descriptor. DO NOT hand-author nested chain / "
            "chain_map step trees — they are fragile to write by hand. Pick a "
            "`flow_type` template and this tool returns the ENTIRE nested flow "
            "tree as a {version, ui} doc, ready to drop into a ```ui-spec "
            "fenced block. The flow then advances entirely client-side with no "
            "further model calls between steps.\n\n"
            "Templates (`flow_type`):\n"
            "- `onboarding_wizard` — pick a goal -> enter workspace details "
            "(branches on the goal) -> confirm. A new-user setup wizard.\n"
            "- `due_diligence_intake` — pick a deal stage -> stage-specific "
            "financials (branches on the stage) -> risk flags -> review. A "
            "multi-step vertical intake; the same shape works for a survey.\n\n"
            "Optional `config` tweaks copy without changing the tree's shape "
            "(e.g. `product_name` for the wizard, `company_name` for the "
            "intake). When in doubt, pass just `flow_type` — the defaults "
            "produce a valid, renderable flow.\n\n"
            "Returns the JSON doc to emit. Wrap it verbatim in a ```ui-spec "
            "fence in your reply — do not edit the chain / chain_map structure."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "flow_type": {
                    "type": "string",
                    "enum": list(FLOW_TYPES),
                    "description": (
                        "Which flow template to scaffold. The builder owns the "
                        "full nested step tree for this template."
                    ),
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Optional domain hint (e.g. 'fintech', 'saas'). "
                        "Reserved for a future data-fresh materialization "
                        "path; does not change the tree today."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Optional per-template copy overrides. Shape-stable: "
                        "tweaks labels/text, never the chain structure. "
                        "onboarding_wizard accepts {product_name, goals}; "
                        "due_diligence_intake accepts {company_name, "
                        "submit_event}."
                    ),
                },
            },
            "required": ["flow_type"],
        }

    async def execute(
        self,
        flow_type: str = "",
        domain: str | None = None,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Expand the descriptor into a `{version, ui}` flow doc (JSON string)."""
        if not flow_type:
            valid = ", ".join(sorted(FLOW_TYPES))
            return self._error(f"start_flow needs a `flow_type`. Known templates: {valid}.")

        # `config` may arrive as a JSON string from subprocess/CLI callers that
        # can't pass a nested object through a flat string signature. Coerce it.
        if isinstance(config, str):
            try:
                config = json.loads(config) if config.strip() else None
            except json.JSONDecodeError:
                return self._error("`config` was a string but not valid JSON.")
        if config is not None and not isinstance(config, dict):
            return self._error("`config` must be an object (key/value map).")

        try:
            doc = build_flow(flow_type, domain=domain, config=config)
        except ValueError as exc:
            # Unknown flow_type — surface the valid set so the model can retry.
            return self._error(str(exc))

        return self._success(json.dumps(doc, ensure_ascii=False))


__all__ = ["StartFlowTool"]
