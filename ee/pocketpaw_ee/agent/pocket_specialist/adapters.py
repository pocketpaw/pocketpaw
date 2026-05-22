# ee/pocketpaw_ee/agent/pocket_specialist/adapters.py
# Created: 2026-05-14 — split the ``pocket_specialist__create`` dispatch
# into two mode-specific adapters. Bumps the historical subagent flow
# into ``SubagentAdapter`` and introduces ``AgentModeAdapter`` for the
# new two-call protocol where the calling chat agent drafts the
# rippleSpec inline using its own LLM and the specialist only runs
# validate-and-persist on the returned draft.
# Modified: 2026-05-22 (feat/bundled-templates, Increment 2a) —
# ``AgentModeAdapter.create`` short-circuits on a ``hints.template_id``:
# it loads the built-in template, passes its ``ripple_spec`` straight to
# ``_validate_and_persist``, and SKIPS the ``_draft_kit`` round-trip. An
# unknown slug falls back to the normal draft-kit flow. Without the
# short-circuit, agent-mode pays two LLM round-trips for a one-shot
# template customization.
# Modified: 2026-05-21 — added full-fledged-app chrome widgets
# (app-shell, sidebar, breadcrumb, sheet, modal, confirm-dialog,
# dropdown-menu, command-palette, coachmark) to the starter list and
# the ``app`` pattern bucket so "build me an app for X" briefs land on
# real chrome instead of composing it from primitives.
# Modified: 2026-05-21 (#1170) — added the edit-side adapters
# (``EditSubagentAdapter``, ``EditAgentModeAdapter``, ``pick_edit_adapter``)
# so the edit endpoint honors ``pocket_specialist_mode`` the same way
# create does. Edit was previously asymmetric — it always spawned a
# backend and ignored agent mode, crashing Claude Code deployments that
# have no ANTHROPIC_API_KEY.
# Modified: 2026-05-21 — ``_apply_ops`` no longer reports
# ``ok=True, action="applied"`` when every supplied op was rejected /
# unknown (zero ops actually applied). That silent-failure state — the
# agent-mode root-replace symptom — now returns ``ok=False,
# action="failed"`` with the reason in ``error`` + ``warnings``.
"""Mode-specific adapters for the pocket specialist's create + edit endpoints.

The MCP tool handlers (``mcp_tool._create_handler`` / ``_edit_handler``)
don't know — and shouldn't care — whether the specialist is spawning a
subagent or piggybacking on the chat agent. They call one of these
adapters via ``pick_adapter`` / ``pick_edit_adapter`` (keyed on
``settings.pocket_specialist_mode``) and get a uniform output back.

Adding a new mode (e.g., ``remote`` calling a hosted spec service):
implement the ``SpecialistCreateAdapter`` / ``SpecialistEditAdapter``
protocol and wire a branch into the matching ``pick_*`` function at the
bottom of this file.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


# Hand-curated starter list of widget kinds the chat agent can reach
# for in agent-mode drafts. NOT exhaustive — the Ripple manifest is
# the source of truth (150 widgets) and the chat agent should use the
# ``mcp__pocketpaw_pocket__get_widget_spec`` tool to look up props for
# any kind it wants. The 10-widget version of this list (flex / grid /
# stat / chart / table / text / button / badge / progress / kanban)
# was provably too narrow — the LLM defaulted to those 10 widgets and
# never reached for the polished layouts already in the library. The
# list below covers the same canonical patterns the showcase at
# https://localhost:5173/showcase demonstrates, organized by use case.
_STARTER_WIDGET_KINDS: tuple[str, ...] = (
    # containers + structure
    "flex",
    "grid",
    "split",
    "tabs",
    "card",
    "section",
    "page-header",
    "hero",
    # full-fledged app shell (use when the brief is "an app for X")
    "app-shell",
    "sidebar",
    "breadcrumb",
    # display
    "text",
    "heading",
    "badge",
    "callout",
    "stat",
    "metric",
    # apps (interactive focal widgets)
    "kanban",
    "calendar",
    "gantt",
    "form-layout",
    "wizard-layout",
    # data viz
    "chart",
    "table",
    "data-grid",
    "audit-log",
    "timeline",
    "kv-table",
    "funnel",
    "heatmap",
    "treemap",
    "gauge",
    "sparkline",
    # polished pattern layouts (Material 3 / HIG canonical shapes)
    "master-detail",
    "entity-detail",
    "pricing-table",
    "invoice-layout",
    "order-status",
    "report-layout",
    "comparison-layout",
    "checklist-layout",
    # high-leverage dashboards (use when pattern=dashboard)
    "pipeline-dashboard",
    "analytics-dashboard",
    "ops-dashboard",
    "project-dashboard",
    "exec-dashboard",
    # rich inputs
    "input",
    "textarea",
    "select",
    "combobox",
    "multi-select",
    "filter-bar",
    "date-picker",
    "location-picker",
    "search",
    # overlays + chrome (UX building blocks for apps)
    "sheet",
    "modal",
    "confirm-dialog",
    "dropdown-menu",
    "command-palette",
    "coachmark",
    # enterprise / advanced
    "comment-thread",
    "tree-table",
    "org-chart",
    "saved-views",
    "notification-center",
    "error-state",
    "empty-state",
)


class SpecialistCreateAdapter(Protocol):
    """Dispatch interface for ``pocket_specialist__create`` request shapes.

    Implementations decide HOW the rippleSpec gets drafted (subagent,
    chat-agent inline, remote service, …). They all return the same
    ``PocketSpecialistCreateOutput`` shape so the MCP tool handler and
    the chat agent don't branch on mode."""

    async def create(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any: ...


class SubagentAdapter:
    """Spawn an isolated backend that runs the specialist's own LLM.

    Wraps the historical flow in ``runtime._run_subagent_pipeline``.
    The runtime keeps that function as the implementation — this
    adapter is the dispatch shim. Importing inside ``create`` avoids
    a circular import between ``adapters`` and ``runtime``.
    """

    async def create(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any:
        from pocketpaw_ee.agent.pocket_specialist.runtime import _run_subagent_pipeline

        return await _run_subagent_pipeline(
            input,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=settings,
        )


class AgentModeAdapter:
    """Two-call protocol — the calling chat agent IS the specialist.

    First call (``input.spec is None``): return a draft kit (structural
    plan echo + rippleSpec shape reminder + widget hint list + next-
    step instructions). The chat agent then drafts the rippleSpec in
    its own context using its own model.

    Second call (``input.spec`` populated): skip the LLM draft phase
    and go straight to validate-and-persist using the same
    ``make_persist_pocket_tool`` the subagent flow uses internally.

    No backend is spawned in either call. ``pocket_specialist_backend``
    and ``pocket_specialist_model`` are ignored entirely; the chat
    agent's already-running model carries the spec-drafting cost.
    """

    async def create(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any:
        started = time.monotonic()
        if input.spec is not None:
            # Second call — the chat agent already drafted a spec.
            return await _validate_and_persist(
                input,
                workspace_id=workspace_id,
                user_id=user_id,
                settings=settings,
                started=started,
            )

        # First call. When the chat agent matched a built-in template,
        # short-circuit: load the template skeleton and persist it
        # directly — no draft-kit round-trip. Agent-mode would otherwise
        # pay two LLM hops for a one-shot template customization.
        template_id = getattr(input.hints, "template_id", None) if input.hints else None
        if template_id:
            template_input = _input_with_template_spec(input, template_id)
            if template_input is not None:
                logger.info(
                    "[pocket-specialist] agent-mode template short-circuit "
                    "(template_id=%s) — skipping draft kit",
                    template_id,
                )
                return await _validate_and_persist(
                    template_input,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    settings=settings,
                    started=started,
                )
            # Unknown slug / load failure — fall through to the draft kit.
            logger.info(
                "[pocket-specialist] agent-mode template_id=%s did not load "
                "— falling back to draft kit",
                template_id,
            )

        return _draft_kit_response(input, started=started)


# ---------------------------------------------------------------------------
# Agent-mode internals
# ---------------------------------------------------------------------------


def _input_with_template_spec(input: Any, template_id: str) -> Any | None:
    """Load a built-in template and return a copy of ``input`` with the
    template's ``ripple_spec`` set as ``spec``.

    Agent mode persists ``input.spec`` directly. By loading the
    template skeleton into ``spec`` here, the create flow takes the
    same validate-and-persist path the second call uses — no LLM round
    trip, no draft kit.

    Returns ``None`` when the slug is unknown or the template files are
    missing / corrupt; the caller then falls back to the draft kit.
    The lazy import keeps ``bundled_templates`` off the hot path for the
    common (no-template) create.
    """
    try:
        from pocketpaw.bundled_templates.loader import load_template
    except Exception:  # noqa: BLE001 — defensive: bundled_templates is OSS core
        logger.warning("[pocket-specialist] bundled_templates.loader import failed", exc_info=True)
        return None

    template = load_template(template_id)
    if template is None:
        return None

    ripple_spec = template.get("ripple_spec")
    if not isinstance(ripple_spec, dict) or not ripple_spec:
        logger.warning(
            "[pocket-specialist] template %r loaded but ripple_spec is empty/invalid",
            template_id,
        )
        return None

    # Strip authoring-only keys (``_placeholder_note`` and any other
    # ``_``-prefixed top-level key) — they are template-author notes,
    # not rippleSpec fields, and must not land in the user's pocket.
    clean_spec = {k: v for k, v in ripple_spec.items() if not k.startswith("_")}

    try:
        return input.model_copy(update={"spec": clean_spec})
    except Exception:  # noqa: BLE001 — model_copy on a non-pydantic input
        logger.warning(
            "[pocket-specialist] could not copy input for template %r", template_id, exc_info=True
        )
        return None


def _draft_kit_response(input: Any, *, started: float) -> Any:
    """Build the first-call response: enough scaffolding for the chat
    agent to draft a rippleSpec inline, without copying the full ~12k-
    token specialist prompt into the chat agent's context.

    The chat agent already has ``mcp__pocketpaw_pocket__get_widget_spec``
    available — the kit tells it to use that for widget props on
    demand rather than inlining the manifest here.
    """
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput

    hints_dict: dict[str, Any] = input.hints.model_dump(exclude_none=True) if input.hints else {}

    kit: dict[str, Any] = {
        "structural_plan": hints_dict,
        "ripple_spec_shape": (
            "A rippleSpec is a JSON tree: the root is typically a "
            "``{type: 'flex', props: {direction, gap, padding}, children: [...]}`` "
            "or a ``{type: 'grid', props: {columns, gap}, children: [...]}``. "
            "Every node has ``type`` (the widget kind) and ``props`` (a flat "
            "dict of allowed props for that kind). Containers add a "
            "``children`` array of nested nodes. Mock data for stat/chart/"
            "table widgets goes directly in props (e.g., ``chart.data`` is a "
            "``[{label, value}]`` list)."
        ),
        "starter_widget_kinds": list(_STARTER_WIDGET_KINDS),
        "rich_widgets_by_pattern": {
            # Pattern → high-leverage widgets that already encapsulate
            # the canonical layout. Reach for these BEFORE composing
            # the same shape from primitives — e.g., use
            # ``pipeline-dashboard`` instead of building a quota progress
            # bar + top reps leaderboard + funnel + recent deals table
            # by hand. The widget exists; let it do the work.
            "dashboard": [
                "pipeline-dashboard",
                "analytics-dashboard",
                "ops-dashboard",
                "project-dashboard",
                "exec-dashboard",
            ],
            "viewer": [
                "entity-detail",
                "pricing-table",
                "invoice-layout",
                "order-status",
                "report-layout",
                "comparison-layout",
            ],
            "app": [
                # Shell — the chrome of a full-fledged app. Reach for
                # these when the brief is "an app for X" (not just a
                # single-widget tool).
                "app-shell",
                "sidebar",
                "tabs",
                "breadcrumb",
                "sheet",
                "modal",
                "command-palette",
                "coachmark",
                # Focal widgets — the WORK happens inside one of these.
                "kanban",
                "calendar",
                "gantt",
                "form-layout",
                "wizard-layout",
                "checklist-layout",
            ],
            "browser": [
                "master-detail",
                "tree-table",
                "filter-bar",
                "saved-views",
            ],
            "wizard": [
                "wizard-layout",
                "checklist-layout",
                "form-layout",
            ],
            "feed": [
                "audit-log",
                "timeline",
                "comment-thread",
                "notification-center",
            ],
        },
        "widget_quality_bar": (
            "If you're tempted to compose a dashboard out of a 3-stat grid "
            "+ a chart + a table, check ``rich_widgets_by_pattern`` first. "
            "The polished domain layout (``pipeline-dashboard`` for sales, "
            "``ops-dashboard`` for incidents, ``project-dashboard`` for "
            "delivery) already gives you the funnel, the leaderboard, the "
            "conversion rates, and the quota progress — composed and styled "
            "to match. Same for viewers: an article isn't ``page-header`` + "
            "``text`` + ``text``, it's ``entity-detail`` or ``report-layout`` "
            "with a body slot. Use the focal widget."
        ),
        "next_step": (
            "Draft a rippleSpec for the structural plan above. Use your own "
            "model — no subagent will be spawned. When ready, call "
            "``pocket_specialist__create`` again with the same brief AND "
            "``spec=<your drafted ripple spec>``. The tool will validate "
            "against the widget manifest and persist the pocket. If "
            "validation returns warnings, the response carries them and you "
            "can call again with a corrected spec."
        ),
        "lookup_tool": (
            "Use ``mcp__pocketpaw_pocket__get_widget_spec`` to fetch allowed "
            "props for any widget kind before drafting (especially the rich "
            "layouts above — they take richer prop shapes than the "
            "primitives). Use ``mcp__pocketpaw_pocket__list_pockets`` to see "
            "existing pockets in the workspace."
        ),
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "[pocket-specialist] agent-mode draft kit returned (hints_keys=%s "
        "starter_kinds=%d duration=%dms)",
        sorted(hints_dict.keys()),
        len(_STARTER_WIDGET_KINDS),
        duration_ms,
    )

    return PocketSpecialistCreateOutput(
        ok=False,
        action="draft_kit",
        pocket=None,
        warnings=[],
        error=None,
        duration_ms=duration_ms,
        backend_used="agent_mode",
        draft_kit=kit,
    )


async def _validate_and_persist(
    input: Any,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
    started: float,
) -> Any:
    """Second-call path: run the spec through the same persist tool the
    subagent uses internally. No LLM in this step — the chat agent
    already did the drafting.

    Reuses ``make_persist_pocket_tool`` so the validation rules, the
    redraft-on-warnings semantics, and the side-channel capture dict
    behave exactly like the subagent flow does. On validation warnings
    the chat agent gets the warnings back and can call once more with
    a corrected spec — mirroring the subagent's internal retry loop.
    """
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput
    from pocketpaw_ee.agent.pocket_specialist.tools import make_persist_pocket_tool

    persist_capture: dict[str, Any] = {}
    tool = make_persist_pocket_tool(
        workspace_id=workspace_id,
        user_id=user_id,
        capture=persist_capture,
        max_validation_retries=settings.pocket_specialist_max_validation_retries,
    )

    hints = input.hints
    tool_args: dict[str, Any] = {
        "ripple_spec": input.spec,
        "name": getattr(hints, "name", None),
        "description": getattr(hints, "description", None),
        "icon": getattr(hints, "icon", None),
        "color": getattr(hints, "color", None),
        "target_pocket_id": getattr(hints, "target_pocket_id", None),
    }

    try:
        await tool.ainvoke(tool_args)
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "[pocket-specialist] agent-mode persist raised (workspace=%s duration=%dms): %s",
            workspace_id,
            duration_ms,
            exc,
        )
        return PocketSpecialistCreateOutput(
            ok=False,
            action="failed",
            pocket=None,
            warnings=list(persist_capture.get("warnings", [])),
            error=f"persist failed: {exc}",
            duration_ms=duration_ms,
            backend_used="agent_mode",
        )

    captured_pocket: dict[str, Any] | None = persist_capture.get("pocket")
    captured_warnings: list[str] = list(persist_capture.get("warnings", []))
    duration_ms = int((time.monotonic() - started) * 1000)

    if captured_pocket is None:
        # ``make_persist_pocket_tool`` short-circuits without saving when
        # the manifest validator returns warnings and the retry budget
        # is unspent. The chat agent should redraft and call again with
        # the same brief + a corrected ``spec``. We return
        # ``action="redraft"`` (distinct from ``"failed"``) so callers
        # can switch on the action and re-prompt the LLM without
        # treating it as a terminal error. ``ok`` stays False because
        # no pocket landed — but the run isn't done, it's waiting on
        # the chat agent's next call.
        logger.info(
            "[pocket-specialist] agent-mode redraft required (warnings=%d duration=%dms)",
            len(captured_warnings),
            duration_ms,
        )
        return PocketSpecialistCreateOutput(
            ok=False,
            action="redraft",
            pocket=None,
            warnings=captured_warnings,
            error=(
                "Spec validation produced warnings — redraft required. "
                "Address each warning and call pocket_specialist__create "
                "again with the corrected spec."
            ),
            duration_ms=duration_ms,
            backend_used="agent_mode",
        )

    action: str = "extended" if hints and hints.target_pocket_id else "created"
    logger.info(
        "[pocket-specialist] agent-mode complete: pocket_id=%s action=%s duration=%dms warnings=%d",
        captured_pocket.get("id", ""),
        action,
        duration_ms,
        len(captured_warnings),
    )
    return PocketSpecialistCreateOutput(
        ok=True,
        action=action,  # type: ignore[arg-type]
        pocket=captured_pocket,
        warnings=captured_warnings,
        duration_ms=duration_ms,
        backend_used="agent_mode",
    )


def pick_adapter(mode: str) -> SpecialistCreateAdapter:
    """Pick the create adapter for the configured specialist mode.

    Unknown modes fall through to the historical subagent adapter so a
    stale config never bricks a deployed instance — the operator sees
    the warning in logs and can correct the value.
    """
    if mode == "agent":
        return AgentModeAdapter()
    if mode != "subagent":
        logger.warning(
            "Unknown pocket_specialist_mode=%r — falling back to subagent. "
            "Valid values: 'subagent', 'agent'.",
            mode,
        )
    return SubagentAdapter()


# ---------------------------------------------------------------------------
# Edit-side adapters — symmetric with the create-side adapters above.
#
# Edit was built asymmetrically (#1170): ``run_edit_specialist`` always
# spawned a backend and never consulted ``pocket_specialist_mode``. On a
# Claude Code deployment (no ANTHROPIC_API_KEY) the default ``deep_agents``
# backend crashes inside LangChain ``ChatAnthropic`` on every edit. These
# adapters give edit the same mode dispatch create already has.
# ---------------------------------------------------------------------------


class SpecialistEditAdapter(Protocol):
    """Dispatch interface for ``pocket_specialist__edit`` request shapes.

    Implementations decide HOW the granular ops get computed (subagent,
    chat-agent inline, …). They all return the same
    ``PocketSpecialistEditOutput`` so the MCP tool handler and the chat
    agent don't branch on mode."""

    async def edit(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any: ...


class EditSubagentAdapter:
    """Spawn an isolated backend that runs the edit specialist's own LLM.

    Wraps the historical flow in ``runtime._run_edit_subagent_pipeline``.
    The runtime keeps that function as the implementation — this adapter
    is the dispatch shim. Importing inside ``edit`` avoids a circular
    import between ``adapters`` and ``runtime``.
    """

    async def edit(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any:
        from pocketpaw_ee.agent.pocket_specialist.runtime import _run_edit_subagent_pipeline

        return await _run_edit_subagent_pipeline(
            input,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=settings,
        )


class EditAgentModeAdapter:
    """Two-call protocol for edits — the calling chat agent IS the
    specialist. The edit-side mirror of ``AgentModeAdapter``.

    First call (``input.ops is None``): return an edit kit (current
    pocket echo + the granular-op vocabulary + next-step instructions).
    The chat agent then decides WHICH granular ops express the intent,
    using its own model.

    Second call (``input.ops`` populated): skip the LLM planning phase
    and apply each op deterministically through the SAME granular tools
    the subagent flow uses internally (``make_edit_pocket_tools``). No
    backend is spawned in either call — ``pocket_specialist_backend`` and
    ``pocket_specialist_model`` are ignored entirely.

    Design choice (#1170): the chat agent hands back GRANULAR OPS, not a
    full mutated rippleSpec. Edit has no whole-spec persist primitive —
    its persistence layer IS the granular ops (each one persists in place
    and emits its own SSE event so the canvas updates live). Reusing those
    ops keeps the per-op SSE updates, the manifest/service validation, and
    the rejected-op semantics ``run_edit_specialist`` already shapes into
    ``warnings``. It also matches create's adapter, which hands the
    persist tool the chat agent's already-computed output rather than a
    diff. The chat agent decides WHAT and WHERE; the adapter applies.
    """

    async def edit(
        self,
        input: Any,
        *,
        workspace_id: str,
        user_id: str,
        settings: Settings,
    ) -> Any:
        started = time.monotonic()
        if input.ops is None:
            return _edit_kit_response(input, started=started)
        return await _apply_ops(input, started=started)


# Granular edit tools the chat agent may reach for in agent mode. Names
# match the tool names produced by ``make_edit_pocket_tools`` — the same
# tools the subagent flow attaches. ``get_pocket`` is intentionally
# omitted: it is a read, and in agent mode the chat agent already has the
# pocket in context.
_GRANULAR_EDIT_OPS: dict[str, str] = {
    "set_state": "Write a value into state at a dotted path. Cheapest data edit.",
    "append_state": "Append an item to an array in state.",
    "remove_state": "Remove a key or array element from state.",
    "patch_state": "Batched top-level merge into state.",
    "set_node_prop": "Set one prop on one widget node (appearance / labels).",
    "set_prop_array_item": "Surgically update one item inside a node's prop array.",
    "append_prop_array_item": "Append one item to a node's prop array.",
    "remove_prop_array_item": "Remove one item from a node's prop array.",
    "add_node": "Add a new widget node into the tree.",
    "replace_node": "Replace a widget node with a new subtree.",
    "move_node": "Move a widget node to a new parent / position.",
    "remove_node": "Remove a widget node from the tree.",
}


def _edit_kit_response(input: Any, *, started: float) -> Any:
    """Build the first-call response: enough scaffolding for the chat
    agent to compute granular ops inline, without spawning a backend.

    The chat agent already holds the heavy edit guidance in the
    ``pocketpaw-edit-pocket`` skill — the kit names the op vocabulary and
    tells it how to call back, it does not re-inline the specialist
    prompt.
    """
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput

    kit: dict[str, Any] = {
        "intent": input.intent,
        "pocket": input.pocket,
        "target_node_ids": input.target_node_ids,
        "granular_ops": _GRANULAR_EDIT_OPS,
        "op_shape": (
            "Each op is ``{op: <name>, args: {...}}``. ``op`` is one of "
            "``granular_ops`` above. Exact args per op: "
            "set_state ``{path, value}``; "
            "set_node_prop ``{node_id, prop, value}``; "
            "add_node ``{parent_id, spec, after_id?, index?}`` — ``spec`` is "
            "the new UINode object (NOT ``node``); position it with "
            "``after_id`` (a sibling node id) or ``index`` (0-based slot), "
            "else it appends; "
            "replace_node ``{node_id, spec}``; "
            "move_node ``{node_id, parent_id, after_id?}``; "
            "remove_node ``{node_id}``; "
            "the prop-array ops take ``{node_id, prop, match}`` (plus "
            "``value`` for set / append). Apply the SMALLEST set of ops "
            "that satisfies the intent."
        ),
        "next_step": (
            "Compute the granular ops for the intent above. Use your own "
            "model — no subagent will be spawned. When ready, call "
            "``pocket_specialist__edit`` again with the same pocket_id + "
            "intent AND ``ops=<your op list>``. Each op is validated and "
            "applied in order; rejected ops come back in ``warnings``."
        ),
        "lookup_tool": (
            "Use ``mcp__pocketpaw_pocket__get_widget_spec`` to confirm a "
            "widget's allowed props before a set_node_prop / add_node op. "
            "If you did not receive the pocket above, read it first."
        ),
    }

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "[pocket-specialist:edit] agent-mode edit kit returned "
        "(pocket_id=%s has_pocket=%s targets=%d duration=%dms)",
        input.pocket_id,
        input.pocket is not None,
        len(input.target_node_ids or []),
        duration_ms,
    )

    return PocketSpecialistEditOutput(
        ok=False,
        action="draft_kit",
        pocket_id=input.pocket_id,
        ops=[],
        duration_ms=duration_ms,
        backend_used="agent_mode",
        draft_kit=kit,
    )


async def _apply_ops(input: Any, *, started: float) -> Any:
    """Second-call path: apply the chat agent's pre-computed granular ops.

    Reuses ``make_edit_pocket_tools`` so every op runs through the exact
    same wrapper, capture, and service-rejection path the subagent flow
    uses. ``_capture_op`` (inside each tool) sorts accepted ops into
    ``capture['ops']`` and rejected ones into ``capture['rejected']`` —
    the runtime's response-shaping logic for ``ops`` / ``warnings`` is
    rebuilt here so the agent-mode output matches the subagent output.
    No LLM runs in this step.
    """
    from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput
    from pocketpaw_ee.agent.pocket_specialist.tools import make_edit_pocket_tools

    ops_capture: dict[str, Any] = {"ops": []}
    tools_by_name = {
        t.name: t for t in make_edit_pocket_tools(pocket_id=input.pocket_id, capture=ops_capture)
    }

    error_msg: str | None = None
    unknown_ops: list[str] = []
    for raw_op in input.ops:
        if not isinstance(raw_op, dict):
            unknown_ops.append(str(raw_op))
            continue
        op_name = raw_op.get("op", "")
        op_args = raw_op.get("args") or {}
        tool = tools_by_name.get(op_name)
        if tool is None:
            # An op the chat agent named that isn't a granular edit tool.
            # Not a crash — record it so it surfaces in warnings, same
            # spirit as a service-rejected op.
            unknown_ops.append(op_name or "<missing op name>")
            logger.warning(
                "[pocket-specialist:edit] agent-mode op %r is not a granular edit tool — skipping",
                op_name,
            )
            continue
        try:
            await tool.ainvoke(op_args)
        except Exception as exc:  # noqa: BLE001
            # A single op blew up — stop and surface it. Ops applied
            # before this one already persisted in place.
            error_msg = f"edit op '{op_name}' failed: {type(exc).__name__}: {exc}"
            logger.warning(
                "[pocket-specialist:edit] agent-mode op %r raised: %s",
                op_name,
                exc,
            )
            break

    duration_ms = int((time.monotonic() - started) * 1000)
    ops = list(ops_capture.get("ops", []))
    rejected = list(ops_capture.get("rejected", []))

    warnings: list[str] = []
    for rej in rejected:
        op_name = rej.get("op", "edit op")
        reason = rej.get("error", "rejected by the service")
        warnings.append(f"Edit op '{op_name}' could not be applied: {reason}")
    for bad in unknown_ops:
        warnings.append(f"Edit op '{bad}' is not a supported granular op and was skipped.")

    # Success accounting. A raised exception (``error_msg``) is a hard
    # failure. But a run can also "fail silently": every op the chat
    # agent supplied got rejected by the service or was unknown, so
    # ZERO ops actually applied. That state must NOT report
    # ``ok=True, action="applied"`` — the canvas never changed, and the
    # caller would believe the edit landed. This was the agent-mode
    # root-replace symptom: ``replace_node`` on the root was rejected by
    # the service, the rejection went into ``warnings``, but the run
    # still claimed ``applied``. Mirrors the #1163 contract — a run that
    # changed nothing is not a success.
    nothing_applied = bool(input.ops) and not ops
    success = error_msg is None and not nothing_applied
    if nothing_applied and error_msg is None:
        error_msg = (
            "No edit ops were applied — every supplied op was rejected or "
            "unsupported. See warnings for the per-op reasons."
        )
    logger.info(
        "[pocket-specialist:edit] agent-mode apply complete: pocket_id=%s "
        "ops=%d rejected=%d unknown=%d success=%s duration=%dms",
        input.pocket_id,
        len(ops),
        len(rejected),
        len(unknown_ops),
        success,
        duration_ms,
    )

    return PocketSpecialistEditOutput(
        ok=success,
        action="applied" if success else "failed",
        pocket_id=input.pocket_id,
        ops=ops,
        duration_ms=duration_ms,
        backend_used="agent_mode",
        error=error_msg,
        warnings=warnings,
    )


def pick_edit_adapter(mode: str) -> SpecialistEditAdapter:
    """Pick the edit adapter for the configured specialist mode.

    Unknown modes fall through to the historical subagent adapter so a
    stale config never bricks a deployed instance — the operator sees
    the warning in logs and can correct the value. Mirrors ``pick_adapter``.
    """
    if mode == "agent":
        return EditAgentModeAdapter()
    if mode != "subagent":
        logger.warning(
            "Unknown pocket_specialist_mode=%r — falling back to subagent "
            "for edit. Valid values: 'subagent', 'agent'.",
            mode,
        )
    return EditSubagentAdapter()


__all__ = [
    "AgentModeAdapter",
    "EditAgentModeAdapter",
    "EditSubagentAdapter",
    "SpecialistCreateAdapter",
    "SpecialistEditAdapter",
    "SubagentAdapter",
    "pick_adapter",
    "pick_edit_adapter",
]
