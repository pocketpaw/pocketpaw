# foresight.py — /foresight surface preamble.
#
# Created: 2026-05-27 — Builds the chat agent's context preamble when the
# user is on a /foresight or /foresight/scenarios/* route. Surfaces:
#   - active scenario run (if ``meta.run_id`` is set) — status, sub_type,
#     ticks, projected-decision count
#   - active custom scenario (if ``meta.scenario_id`` is set) — name,
#     sub_type, persona + tick counts
#   - workspace ambient state — recent run count + latest backtest gate
#   - the foresight-create-sim skill activation hint, when
#     ``settings.foresight_use_skill`` is True (default ON as of 2026-05-27).
#
# Failure modes degrade — any query exception logs at ``debug`` and gets
# omitted from the preamble. The chat path receives a minimal surface
# tag even when every query fails (better than no preamble + a 5xx).

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


_VALID_PANELS = {"scenarios", "live", "results", "aggregate", "insights", "editor"}


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /foresight surface preamble.

    Always returns a string. Individual data fetches are wrapped in
    isolated try/except so a single missing collection (e.g. backtests
    not yet seeded) doesn't drop the whole preamble.
    """
    panel = meta.panel if meta.panel in _VALID_PANELS else None
    surface_tag = (
        f'<surface kind="foresight" route="/foresight"'
        f'{f" panel=\"{panel}\"" if panel else ""} />'
    )
    # Directive block — assert foresight-first behavior FIRST so the
    # agent's default "let me offer some pocket buttons" pattern doesn't
    # leak into the greeting. Captain caught this 2026-05-27: agent
    # responded to "hi" on /foresight with [Build a pocket / List my
    # pockets / Rehearse a decision] because the preamble was descriptive
    # but not directive. This block is small + assertive — the agent
    # reads it before the rest of the chat-agent system prompt's pocket
    # guidance.
    guidance = (
        "<surface-guidance>\n"
        "The user is on the Foresight rail (population simulation for\n"
        "decision rehearsal). PREFER Foresight affordances:\n"
        "  - Create / edit a scenario (use the foresight-create-sim skill).\n"
        "  - Run a scenario + summarize results.\n"
        "  - Explain projected decisions, calibration accuracy, insights.\n"
        "  - Branch a run, promote a decision to an anchor.\n"
        "DO NOT offer pocket creation, pocket lists, or generic canvas\n"
        "actions in starter buttons here — those belong on /pockets, not\n"
        "/foresight. If the user explicitly asks for a pocket, you may\n"
        "redirect them; otherwise stay in the Foresight context. When\n"
        "greeting, offer Foresight-shaped starter actions only:\n"
        '  - "Rehearse a decision"\n'
        '  - "Run a quick scenario"\n'
        '  - "Show me my recent runs"\n'
        '  - "Explain the latest insights"\n'
        "</surface-guidance>"
    )
    parts: list[str] = [surface_tag, guidance]

    # Active run block. Pulled when the sidebar stamps run_id (e.g. on
    # /foresight when the rail is watching a specific run).
    if meta.run_id:
        run_block = await _render_active_run(workspace_id, meta.run_id)
        if run_block:
            parts.append(run_block)

    # Active scenario block. Pulled when the user is in the editor
    # (/foresight/scenarios/[id]) so the agent knows what they're editing.
    if meta.scenario_id:
        scenario_block = await _render_active_scenario(workspace_id, meta.scenario_id)
        if scenario_block:
            parts.append(scenario_block)

    # Workspace ambient — recent run count + latest gate decision. Cheap
    # signal that helps the agent gauge "is this a new workspace or one
    # with a history?".
    ambient = await _render_workspace_ambient(workspace_id)
    if ambient:
        parts.append(ambient)

    # Skill activation hint. The bundled ``foresight-create-sim`` skill
    # auto-installs to ``~/.claude/skills/`` regardless of this flag;
    # surfacing the hint here tells the agent to prefer the skill path
    # for scenario create / edit / run.
    skill_block = _render_skill_hint()
    if skill_block:
        parts.append(skill_block)

    return truncate_preamble("\n".join(parts))


async def _render_active_run(workspace_id: str, run_id: str) -> str:
    """Snapshot of the run the user is currently viewing.

    Looks up the ForesightRun doc + counts ForesightProjectedDecisions
    landed so far. Both queries tenant-filtered on workspace_id (cloud
    rule #7).
    """
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.foresight import service as foresight_service
    except Exception:
        logger.debug("foresight handler: service import failed", exc_info=True)
        return ""

    try:
        ctx = RequestContext(
            user_id="surface-foresight",
            workspace_id=workspace_id,
            request_id="surface-foresight-run",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        run = await foresight_service.get_scenario_run(ctx, run_id)
    except Exception:
        logger.debug("foresight handler: get_scenario_run failed for %s", run_id, exc_info=True)
        return ""

    if run is None:
        return ""

    status = getattr(run, "status", "unknown")
    sub_type = getattr(run, "sub_type", "unknown")
    ticks_completed = getattr(run, "ticks_completed", None)
    total_ticks = getattr(run, "total_ticks", None)
    tick_progress = (
        f"{ticks_completed}/{total_ticks}" if (ticks_completed is not None and total_ticks) else "—"
    )

    # Projected-decision count — small surface signal so the agent can
    # answer "how many decisions has this run produced so far?" without
    # round-tripping a separate query.
    try:
        decisions = await foresight_service.list_projected_decisions(
            ctx, run_id, limit=1, offset=0
        )
        decision_count = getattr(decisions, "total", 0) if decisions else 0
    except Exception:
        decision_count = 0

    return (
        f'<active-run id="{run_id}" status="{status}" sub_type="{sub_type}" '
        f'ticks="{tick_progress}" projected_decisions="{decision_count}" />'
    )


async def _render_active_scenario(workspace_id: str, scenario_id: str) -> str:
    """Snapshot of the custom scenario the user is editing."""
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
    except Exception:
        logger.debug("foresight handler: scenarios import failed", exc_info=True)
        return ""

    try:
        ctx = RequestContext(
            user_id="surface-foresight",
            workspace_id=workspace_id,
            request_id="surface-foresight-scenario",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        scenario = await foresight_scenarios.get_custom_scenario(ctx, scenario_id)
    except Exception:
        logger.debug(
            "foresight handler: get_custom_scenario failed for %s", scenario_id, exc_info=True
        )
        return ""

    if scenario is None:
        return ""

    name = getattr(scenario, "name", "(unnamed)")
    sub_type = getattr(scenario, "sub_type", "unknown")
    parsed = getattr(scenario, "parsed_meta", None)
    personas = getattr(parsed, "num_personas", 0) if parsed else 0
    ticks = getattr(parsed, "num_ticks", 0) if parsed else 0

    return (
        f'<active-scenario id="{scenario_id}" name="{name}" sub_type="{sub_type}" '
        f'personas="{personas}" ticks="{ticks}" />'
    )


async def _render_workspace_ambient(workspace_id: str) -> str:
    """Lightweight workspace context — recent run count + gate state."""
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
        from pocketpaw_ee.cloud.foresight import service as foresight_service
    except Exception:
        return ""

    try:
        ctx = RequestContext(
            user_id="surface-foresight",
            workspace_id=workspace_id,
            request_id="surface-foresight-ambient",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )
        runs = await foresight_service.list_scenario_runs(ctx, limit=5, offset=0)
        run_count = getattr(runs, "total", 0) if runs else 0
    except Exception:
        run_count = 0

    try:
        gate = await foresight_service.get_onboarding_gate(ctx)
        gate_state = getattr(gate, "reason", "unknown") if gate else "unknown"
    except Exception:
        gate_state = "unknown"

    return (
        f'<workspace-summary recent_runs="{run_count}" '
        f'onboarding_gate="{gate_state}" />'
    )


def _render_skill_hint() -> str:
    """Skill activation hint — opt-in via ``settings.foresight_use_skill``.

    The bundled ``foresight-create-sim`` skill auto-installs to
    ``~/.claude/skills/`` regardless of this flag. Surfacing the hint
    here tells the agent to PREFER the skill path when the user asks
    to create / edit / run a scenario from chat, instead of describing
    the steps in prose.
    """
    try:
        from pocketpaw.config import get_settings

        settings = get_settings()
    except Exception:
        return ""

    if not getattr(settings, "foresight_use_skill", False):
        return ""

    return (
        '<skill-active name="foresight-create-sim">\n'
        "When the user asks to rehearse / simulate / forecast / branch a\n"
        "decision, use the foresight-create-sim skill from\n"
        "~/.claude/skills/foresight-create-sim/. Drive the cloud CRUD via\n"
        "Bash + curl with the X-PocketPaw-Internal header trio. The skill\n"
        "covers the YAML schema, the 422 envelope shape, and three worked\n"
        "examples (Decision Forecast, Market Sim, Org Change).\n"
        "</skill-active>"
    )


__all__ = ["build_preamble"]
