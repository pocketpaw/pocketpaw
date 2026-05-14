# ee/agent/pocket_specialist/adapters.py
# Created: 2026-05-14 — split the ``pocket_specialist__create`` dispatch
# into two mode-specific adapters. Bumps the historical subagent flow
# into ``SubagentAdapter`` and introduces ``AgentModeAdapter`` for the
# new two-call protocol where the calling chat agent drafts the
# rippleSpec inline using its own LLM and the specialist only runs
# validate-and-persist on the returned draft.
"""Mode-specific adapters for the pocket specialist's create endpoint.

The MCP tool handler (``mcp_tool._create_handler``) doesn't know — and
shouldn't care — whether the specialist is spawning a subagent or
piggybacking on the chat agent. It calls one of these adapters via
``pick_adapter(settings.pocket_specialist_mode)`` and gets a uniform
``PocketSpecialistCreateOutput`` back.

Adding a new mode (e.g., ``remote`` calling a hosted spec service):
implement the ``SpecialistCreateAdapter`` protocol and wire a branch
into ``pick_adapter`` at the bottom of this file.
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
    "flex", "grid", "split", "tabs", "card", "section", "page-header", "hero",
    # display
    "text", "heading", "badge", "callout", "stat", "metric",
    # apps (interactive focal widgets)
    "kanban", "calendar", "gantt", "form-layout", "wizard-layout",
    # data viz
    "chart", "table", "data-grid", "audit-log", "timeline", "kv-table",
    "funnel", "heatmap", "treemap", "gauge", "sparkline",
    # polished pattern layouts (Material 3 / HIG canonical shapes)
    "master-detail", "entity-detail", "pricing-table", "invoice-layout",
    "order-status", "report-layout", "comparison-layout", "checklist-layout",
    # high-leverage dashboards (use when pattern=dashboard)
    "pipeline-dashboard", "analytics-dashboard", "ops-dashboard",
    "project-dashboard", "exec-dashboard",
    # rich inputs
    "input", "textarea", "select", "combobox", "multi-select", "filter-bar",
    "date-picker", "location-picker", "search",
    # enterprise / advanced
    "comment-thread", "tree-table", "org-chart", "saved-views",
    "notification-center", "error-state", "empty-state",
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
    ) -> Any:
        ...


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
        from ee.agent.pocket_specialist.runtime import _run_subagent_pipeline

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
        from ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput

        started = time.monotonic()
        if input.spec is None:
            return _draft_kit_response(input, started=started)
        return await _validate_and_persist(
            input,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=settings,
            started=started,
        )


# ---------------------------------------------------------------------------
# Agent-mode internals
# ---------------------------------------------------------------------------


def _draft_kit_response(input: Any, *, started: float) -> Any:
    """Build the first-call response: enough scaffolding for the chat
    agent to draft a rippleSpec inline, without copying the full ~12k-
    token specialist prompt into the chat agent's context.

    The chat agent already has ``mcp__pocketpaw_pocket__get_widget_spec``
    available — the kit tells it to use that for widget props on
    demand rather than inlining the manifest here.
    """
    from ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput

    hints_dict: dict[str, Any] = (
        input.hints.model_dump(exclude_none=True) if input.hints else {}
    )

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
    from ee.agent.pocket_specialist.runtime import PocketSpecialistCreateOutput
    from ee.agent.pocket_specialist.tools import make_persist_pocket_tool

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
            "[pocket-specialist] agent-mode persist raised "
            "(workspace=%s duration=%dms): %s",
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
            "[pocket-specialist] agent-mode redraft required "
            "(warnings=%d duration=%dms)",
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
        "[pocket-specialist] agent-mode complete: pocket_id=%s action=%s "
        "duration=%dms warnings=%d",
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


__all__ = [
    "AgentModeAdapter",
    "SpecialistCreateAdapter",
    "SubagentAdapter",
    "pick_adapter",
]
