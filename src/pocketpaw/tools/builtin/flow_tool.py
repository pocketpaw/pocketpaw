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
    FlowBuildError,
    build_flow,
    build_flow_from_descriptor,
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
        return (
            "Scaffold a complete multi-step flow OR interactive mini-app (a "
            "wizard, intake, survey, onboarding sequence, or a 'collect "
            "details then DO something' flow) from a FLAT step-graph. DO NOT "
            "hand-author nested chain / chain_map step trees — they are fragile "
            "to write by hand. You describe a flat list of steps; this tool "
            "materializes the ENTIRE nested, validated flow tree as a "
            "{version, ui} doc, ready to drop into a ```ui-spec fence. The flow "
            "then advances entirely client-side with no further model calls "
            "between steps.\n\n"
            "AUTHOR A FLAT GRAPH (think in states, not screens):\n"
            "- `flow`: a stable id for the flow (e.g. 'vendor_intake').\n"
            "- `entry`: the id of the first step.\n"
            "- `steps`: a list. Each step has an `id`, a `kind` "
            "(select | form | confirm | info), a `title`, its content "
            "(`options` for select, `fields` for form, `review` for confirm, "
            "`body` for info), and where it goes next:\n"
            '    * `next: "<id>"` — linear next step;\n'
            '    * `branch: { "<optionId>": "<id>" }` — branch on the '
            "picked option.\n"
            "  A step with NEITHER `next` nor `branch` is the TERMINAL step and "
            "carries `complete` (what to do with the answers):\n"
            "    chat → hand the answers back to you (default); navigate / emit "
            "→ go somewhere / raise an event; call_binding → write to the "
            "backend; create_pocket → materialize a permanent pocket; "
            "invoke_tool → run a tool (may be unavailable until the tool "
            "registry ships).\n"
            "- Per-step `actions` (optional) are buttons that call a "
            "tool/API/binding MID-FLOW without leaving the step (verb: "
            "call_binding | api | invoke_tool).\n"
            "- Reference earlier answers with `{stepId.field}` "
            "(e.g. `{pick_goal.label}`, `{enter_details.company}`) in review "
            "rows and action args — this tool rewrites them correctly; you "
            "NEVER write the raw `{state.…_selection/_formData}` form.\n\n"
            "The builder REJECTS any graph that would dead-end (a select step "
            "that goes nowhere, a dangling transition, a missing terminal, an "
            "unknown verb) with a precise error you can fix and retry.\n\n"
            "PRESET SHORTHAND (optional): instead of `steps`, you may pass a "
            "`flow_type` for one of the two known shapes — 'onboarding_wizard' "
            "or 'due_diligence_intake' — plus an optional `config` for copy "
            "tweaks. The flat `steps` graph is the general path; `flow_type` is "
            "a convenience for those two exact shapes.\n\n"
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
                "flow": {
                    "type": "string",
                    "description": (
                        "A stable id for the flow (e.g. 'vendor_intake'). Seeds "
                        "each step's flowId namespace. Required when authoring a "
                        "flat `steps` graph."
                    ),
                },
                "entry": {
                    "type": "string",
                    "description": (
                        "The id of the FIRST step (must exist in `steps`). "
                        "Required when authoring a flat `steps` graph."
                    ),
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "The flat step-graph: a list of step objects. Each step "
                        "has `id`, `kind` (select | form | confirm | info), "
                        "`title`, its kind-specific content (`options` / "
                        "`fields` / `review` / `body`), and a transition "
                        "(`next` or `branch`) — OR `complete` if it is the "
                        "terminal step. Optional per-step `actions` run a "
                        "tool/API mid-flow. This is the general authoring path."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Optional title shown on the flow frame.",
                },
                "complete": {
                    "type": "object",
                    "description": (
                        "Optional flow-level terminal default — the `complete` "
                        "action a terminal step inherits when it declares none "
                        "of its own (e.g. {action:'chat', message:'…'})."
                    ),
                },
                "flow_type": {
                    "type": "string",
                    "enum": list(FLOW_TYPES),
                    "description": (
                        "OPTIONAL preset shorthand for the two known shapes "
                        "('onboarding_wizard' | 'due_diligence_intake') instead "
                        "of authoring a flat `steps` graph. The builder owns the "
                        "full nested tree for this preset."
                    ),
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Optional domain hint (e.g. 'fintech', 'saas') for the "
                        "preset path. Reserved for a future data-fresh "
                        "materialization path; does not change the tree today."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Optional per-preset copy overrides (preset path only). "
                        "Shape-stable: tweaks labels/text, never the chain "
                        "structure. onboarding_wizard accepts {product_name, "
                        "goals, complete_message}; due_diligence_intake accepts "
                        "{company_name, complete_message}."
                    ),
                },
            },
            # Nothing is hard-required at the schema level: the general path
            # needs flow+entry+steps, the preset path needs flow_type. `execute`
            # validates the combination and returns an agent-readable error when
            # neither is supplied.
            "required": [],
        }

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
