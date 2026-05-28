# ee/pocketpaw_ee/agent/mcp_servers/foresight.py
# Created: 2026-05-28 — in-process SDK MCP server for Foresight scenario
# CRUD + run. Closes the bug where the bundled ``foresight-create-sim``
# skill told the claude_agent_sdk chat agent to call the cloud REST
# surface with ``$WORKSPACE_ID`` / ``$USER_ID`` env vars that are never
# set in that backend. Typed MCP tools close over the chat session's
# workspace id (read from ``ee.cloud.chat.agent_service`` ContextVars)
# so the agent literally cannot get the workspace wrong.
#
# Mirrors the ``pocketpaw_pocket`` server shape exactly:
#   - SERVER_NAME constant + per-tool ``mcp__<server>__<tool>`` ids
#   - FORESIGHT_TOOL_IDS tuple for the claude_sdk allowlist
#   - ``_result_payload`` / ``_error`` helpers that translate the
#     ``{ok, ...}`` envelope from ``ee.cloud.foresight.agent_context``
#     into MCP tool-result shape
#   - ``build_foresight_server`` returns ``None`` when claude_agent_sdk
#     isn't installed (same import-guard pattern the sibling servers use)
#
# Tools (14): list_scenarios, get_scenario, save_scenario, update_scenario,
# delete_scenario, run_scenario, list_runs, get_run, plus the 2026-05-28
# result-side reads — list_projected_decisions, get_aggregate,
# get_insights — and the 2026-05-28 backtest-read tools —
# list_backtests, get_backtest, get_onboarding_gate. The backtest tools
# are READ-ONLY by design (RFC 08 §13.1 — backtest creation needs
# ground-truth anchors and ships through the dashboard Aggregate panel).

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_foresight"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries on the claude_sdk backend must use this exact form.
LIST_SCENARIOS_TOOL_ID = f"mcp__{SERVER_NAME}__list_scenarios"
GET_SCENARIO_TOOL_ID = f"mcp__{SERVER_NAME}__get_scenario"
SAVE_SCENARIO_TOOL_ID = f"mcp__{SERVER_NAME}__save_scenario"
UPDATE_SCENARIO_TOOL_ID = f"mcp__{SERVER_NAME}__update_scenario"
DELETE_SCENARIO_TOOL_ID = f"mcp__{SERVER_NAME}__delete_scenario"
RUN_SCENARIO_TOOL_ID = f"mcp__{SERVER_NAME}__run_scenario"
LIST_RUNS_TOOL_ID = f"mcp__{SERVER_NAME}__list_runs"
GET_RUN_TOOL_ID = f"mcp__{SERVER_NAME}__get_run"
# Result reads — added 2026-05-28 follow-up to PR #1266. Closes the same
# "agent fell back to curl with bad env vars" gap on the read side.
LIST_PROJECTED_DECISIONS_TOOL_ID = f"mcp__{SERVER_NAME}__list_projected_decisions"
GET_AGGREGATE_TOOL_ID = f"mcp__{SERVER_NAME}__get_aggregate"
GET_INSIGHTS_TOOL_ID = f"mcp__{SERVER_NAME}__get_insights"
# Backtest-side reads — 2026-05-28 follow-up. Read-only per RFC 08 §13.1
# (backtest creation stays UI-initiated; the chat surface can't reliably
# produce ground-truth anchors). list_backtests + get_backtest cover the
# "did we backtest yet?" / "show me past backtests" / "what was the gate
# decision?" path; get_onboarding_gate covers "are we unlocked?".
LIST_BACKTESTS_TOOL_ID = f"mcp__{SERVER_NAME}__list_backtests"
GET_BACKTEST_TOOL_ID = f"mcp__{SERVER_NAME}__get_backtest"
GET_ONBOARDING_GATE_TOOL_ID = f"mcp__{SERVER_NAME}__get_onboarding_gate"

FORESIGHT_TOOL_IDS = (
    LIST_SCENARIOS_TOOL_ID,
    GET_SCENARIO_TOOL_ID,
    SAVE_SCENARIO_TOOL_ID,
    UPDATE_SCENARIO_TOOL_ID,
    DELETE_SCENARIO_TOOL_ID,
    RUN_SCENARIO_TOOL_ID,
    LIST_RUNS_TOOL_ID,
    GET_RUN_TOOL_ID,
    LIST_PROJECTED_DECISIONS_TOOL_ID,
    GET_AGGREGATE_TOOL_ID,
    GET_INSIGHTS_TOOL_ID,
    LIST_BACKTESTS_TOOL_ID,
    GET_BACKTEST_TOOL_ID,
    GET_ONBOARDING_GATE_TOOL_ID,
)

_SUB_TYPE_ENUM = ["decision_forecast", "market_sim", "org_change_rehearsal"]


def _result_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Translate an ``agent_context`` ``{ok, ...}`` envelope into the MCP
    response shape. On error, surface the code + message verbatim so the
    agent can decide whether to retry or surface to the user."""
    if not result.get("ok"):
        error_code = result.get("error", "unknown_error")
        message = result.get("message", "")
        text = f"Error: {error_code}" + (f" — {message}" if message else "")
        return {"content": [{"type": "text", "text": text}], "is_error": True}
    # Drop the ``ok`` flag from the payload the agent reads — it's a
    # transport concern, not a wire-shape concern.
    body = {k: v for k, v in result.items() if k != "ok"}
    return {
        "content": [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    }


def _error(text: str) -> dict[str, Any]:
    """Build an MCP error response. The agent reads ``text`` and retries."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


# ---------------------------------------------------------------------------
# Tool handlers — each is a thin shim into the agent_context wrappers.
# ---------------------------------------------------------------------------


async def _list_scenarios_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import list_scenarios_for_agent

    limit = args.get("limit", 20)
    offset = args.get("offset", 0)
    sub_type = args.get("sub_type")
    return _result_payload(
        await list_scenarios_for_agent(limit=limit, offset=offset, sub_type=sub_type)
    )


async def _get_scenario_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import get_scenario_for_agent

    scenario_id = args.get("scenario_id")
    if not scenario_id or not isinstance(scenario_id, str):
        return _error("get_scenario requires a `scenario_id` (string).")
    return _result_payload(await get_scenario_for_agent(scenario_id))


async def _save_scenario_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import save_scenario_for_agent

    name = args.get("name")
    sub_type = args.get("sub_type")
    yaml_body = args.get("yaml_body")
    description = args.get("description")
    if not name or not isinstance(name, str):
        return _error("save_scenario requires a `name` (string).")
    if not sub_type or not isinstance(sub_type, str):
        return _error("save_scenario requires a `sub_type` (string).")
    if not yaml_body or not isinstance(yaml_body, str):
        return _error(
            "save_scenario requires a `yaml_body` (string). Pass the full "
            "scenario YAML — the inner schema (personas, n_ticks, "
            "tier_mix, ...) is documented in the foresight-create-sim skill."
        )
    return _result_payload(
        await save_scenario_for_agent(
            name=name, sub_type=sub_type, yaml_body=yaml_body, description=description
        )
    )


async def _update_scenario_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import update_scenario_for_agent

    scenario_id = args.get("scenario_id")
    name = args.get("name")
    sub_type = args.get("sub_type")
    yaml_body = args.get("yaml_body")
    description = args.get("description")
    if not scenario_id or not isinstance(scenario_id, str):
        return _error("update_scenario requires a `scenario_id` (string).")
    if not name or not isinstance(name, str):
        return _error("update_scenario requires a `name` (string).")
    if not sub_type or not isinstance(sub_type, str):
        return _error("update_scenario requires a `sub_type` (string).")
    if not yaml_body or not isinstance(yaml_body, str):
        return _error(
            "update_scenario requires a `yaml_body` (string) — PUT is a "
            "full replace; GET first and pass back the FULL body with the "
            "user's edits applied."
        )
    return _result_payload(
        await update_scenario_for_agent(
            scenario_id=scenario_id,
            name=name,
            sub_type=sub_type,
            yaml_body=yaml_body,
            description=description,
        )
    )


async def _delete_scenario_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import delete_scenario_for_agent

    scenario_id = args.get("scenario_id")
    if not scenario_id or not isinstance(scenario_id, str):
        return _error("delete_scenario requires a `scenario_id` (string).")
    return _result_payload(await delete_scenario_for_agent(scenario_id))


async def _run_scenario_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import run_scenario_for_agent

    name = args.get("name")
    custom_scenario_id = args.get("custom_scenario_id")
    route_to_instinct = bool(args.get("route_to_instinct", False))
    precedent_seed = args.get("precedent_seed")
    if not name or not isinstance(name, str):
        return _error("run_scenario requires a `name` (string).")
    if not custom_scenario_id or not isinstance(custom_scenario_id, str):
        return _error(
            "run_scenario requires a `custom_scenario_id` (string). Save "
            "the scenario via save_scenario first, then pass the returned "
            "id here — the chat surface only supports saved-scenario runs."
        )
    return _result_payload(
        await run_scenario_for_agent(
            name=name,
            custom_scenario_id=custom_scenario_id,
            route_to_instinct=route_to_instinct,
            precedent_seed=precedent_seed,
        )
    )


async def _list_runs_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import list_runs_for_agent

    limit = args.get("limit", 10)
    offset = args.get("offset", 0)
    return _result_payload(await list_runs_for_agent(limit=limit, offset=offset))


async def _get_run_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import get_run_for_agent

    run_id = args.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return _error("get_run requires a `run_id` (string).")
    return _result_payload(await get_run_for_agent(run_id))


async def _list_projected_decisions_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import list_projected_decisions_for_agent

    run_id = args.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return _error(
            "list_projected_decisions requires a `run_id` (string). Find it "
            "via list_runs or capture it from run_scenario's response."
        )
    anchor_id = args.get("anchor_id")
    if anchor_id is not None and not isinstance(anchor_id, str):
        return _error("list_projected_decisions `anchor_id` must be a string when set.")
    limit = args.get("limit", 50)
    offset = args.get("offset", 0)
    return _result_payload(
        await list_projected_decisions_for_agent(
            run_id, anchor_id=anchor_id, limit=limit, offset=offset
        )
    )


async def _get_aggregate_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import get_aggregate_for_agent

    window_days = args.get("window_days")
    if window_days is not None and not isinstance(window_days, int):
        return _error("get_aggregate `window_days` must be an integer when set.")
    return _result_payload(await get_aggregate_for_agent(window_days=window_days))


async def _get_insights_handler(args: dict) -> dict:
    # Intentionally ignores ``args`` — get_insights takes no parameters
    # (the synthesizer reads the workspace's full window).
    del args
    from pocketpaw_ee.cloud.foresight.agent_context import get_insights_for_agent

    return _result_payload(await get_insights_for_agent())


# ---------------------------------------------------------------------------
# Backtest-side handlers — read-only per RFC 08 §13.1. Backtest creation
# stays in the dashboard Aggregate panel because it needs ground-truth
# anchors the chat surface can't reliably produce.
# ---------------------------------------------------------------------------


async def _list_backtests_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import list_backtests_for_agent

    limit = args.get("limit", 10)
    offset = args.get("offset", 0)
    return _result_payload(await list_backtests_for_agent(limit=limit, offset=offset))


async def _get_backtest_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.foresight.agent_context import get_backtest_for_agent

    backtest_id = args.get("backtest_id")
    if not backtest_id or not isinstance(backtest_id, str):
        return _error(
            "get_backtest requires a `backtest_id` (string). Find it via "
            "list_backtests."
        )
    return _result_payload(await get_backtest_for_agent(backtest_id))


async def _get_onboarding_gate_handler(args: dict) -> dict:
    # Intentionally ignores ``args`` — the gate read takes no parameters
    # (the workspace is inferred from the active chat stream).
    del args
    from pocketpaw_ee.cloud.foresight.agent_context import get_onboarding_gate_for_agent

    return _result_payload(await get_onboarding_gate_for_agent())


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_foresight_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for Foresight, or ``None`` if
    the Claude Agent SDK isn't installed. Same import-guard the sibling
    servers use — a missing SDK is non-fatal for the rest of the runtime.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_foresight MCP disabled")
        return None

    @tool(
        "list_scenarios",
        (
            "List the active workspace's saved Foresight scenarios. Call this "
            "FIRST on every create flow — discovery prevents duplicates. "
            "Workspace context is automatic; no headers, no ids to pass in. "
            "Returns ``items[]`` (id, name, sub_type, num_personas, num_ticks, "
            "updated_at) plus pagination meta (total, limit, offset, has_more)."
        ),
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items (default 20, cap 100)."},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)."},
                "sub_type": {
                    "type": "string",
                    "enum": _SUB_TYPE_ENUM,
                    "description": (
                        "Optional filter on scenario sub-type. Omit to list across all sub-types."
                    ),
                },
            },
            "required": [],
        },
    )
    async def list_scenarios(args):  # type: ignore[no-untyped-def]
        return await _list_scenarios_handler(args)

    @tool(
        "get_scenario",
        (
            "Fetch one saved scenario by id. Returns the full yaml_body + "
            "parsed_meta (num_personas, num_ticks, tier_mix, precedent_seed) "
            "so an edit flow can read-modify-write without losing fields. "
            "404 (``foresight_custom_scenario.not_found``) for unknown or "
            "cross-tenant ids — the workspace is inferred from the chat stream."
        ),
        {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string", "description": "Id from list_scenarios items."}
            },
            "required": ["scenario_id"],
        },
    )
    async def get_scenario(args):  # type: ignore[no-untyped-def]
        return await _get_scenario_handler(args)

    @tool(
        "save_scenario",
        (
            "Persist a new custom scenario in the active workspace. Returns "
            "201 with the full scenario object including the new `id` — "
            "CAPTURE THE ID so a follow-up run_scenario call can reference "
            "it. ``yaml_body`` is the full scenario YAML as a string; the "
            "schema (personas[], n_ticks, tier_mix, precedent_seed, ...) is "
            "documented in the foresight-create-sim skill. 422 surfaces YAML "
            "parse errors, persona/tick caps, tier-mix sum, and sub_type "
            "mismatches with stable codes (foresight.invalid_yaml, "
            "foresight.invalid_scenario, foresight.sub_type_mismatch)."
        ),
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable scenario label."},
                "sub_type": {
                    "type": "string",
                    "enum": _SUB_TYPE_ENUM,
                    "description": (
                        "Scenario sub-type. Must match the `sub_type:` declared "
                        "inside the YAML body or the service returns 422 "
                        "foresight.sub_type_mismatch."
                    ),
                },
                "yaml_body": {
                    "type": "string",
                    "description": "Full scenario YAML (≤64 KB).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional short description (≤500 chars).",
                },
            },
            "required": ["name", "sub_type", "yaml_body"],
        },
    )
    async def save_scenario(args):  # type: ignore[no-untyped-def]
        return await _save_scenario_handler(args)

    @tool(
        "update_scenario",
        (
            "Full-REPLACE a saved scenario. PUT semantics — every field on "
            "the body overwrites the saved doc. ALWAYS get_scenario first, "
            "mutate only the fields the user named, then call this with the "
            "complete body. Validation is identical to save_scenario."
        ),
        {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string", "description": "Id of the scenario to update."},
                "name": {"type": "string"},
                "sub_type": {"type": "string", "enum": _SUB_TYPE_ENUM},
                "yaml_body": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["scenario_id", "name", "sub_type", "yaml_body"],
        },
    )
    async def update_scenario(args):  # type: ignore[no-untyped-def]
        return await _update_scenario_handler(args)

    @tool(
        "delete_scenario",
        (
            "Remove a saved scenario. Idempotency: a second delete on the "
            "same id returns 404 (``foresight_custom_scenario.not_found``). "
            "ASK THE USER BEFORE CALLING — no undo path."
        ),
        {
            "type": "object",
            "properties": {
                "scenario_id": {"type": "string", "description": "Id to delete."},
            },
            "required": ["scenario_id"],
        },
    )
    async def delete_scenario(args):  # type: ignore[no-untyped-def]
        return await _delete_scenario_handler(args)

    @tool(
        "run_scenario",
        (
            "Execute a saved scenario. ``custom_scenario_id`` is REQUIRED — "
            "the chat surface ONLY supports the saved-scenario path so the "
            "scenario stays re-runnable from the dashboard. Save first via "
            "save_scenario, capture the id, then call this. Returns the full "
            "run record (id, status, result.aggregates, "
            "result.projected_decisions[]). v0.1 backend completes "
            "synchronously; future versions return status=queued."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Run label (usually the scenario's display name).",
                },
                "custom_scenario_id": {
                    "type": "string",
                    "description": "Id from save_scenario / list_scenarios.",
                },
                "route_to_instinct": {
                    "type": "boolean",
                    "description": (
                        "When true, every ProjectedDecision the run emits also "
                        "lands one row in the Instinct approval queue. "
                        "Default false."
                    ),
                },
                "precedent_seed": {
                    "type": "string",
                    "description": (
                        "Optional global forward-precedent seed. Omit unless the "
                        "user explicitly asked to link projections to past decisions."
                    ),
                },
            },
            "required": ["name", "custom_scenario_id"],
        },
    )
    async def run_scenario(args):  # type: ignore[no-untyped-def]
        return await _run_scenario_handler(args)

    @tool(
        "list_runs",
        (
            "List recent scenario runs in the active workspace, newest first. "
            "Use this to find a previous run the user is referring to ('show "
            "me the renewal sim from yesterday') without scanning the full "
            "result blob. Returns lightweight items (id, scenario_name, "
            "status, created_at, error)."
        ),
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items (default 10, cap 200)."},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)."},
            },
            "required": [],
        },
    )
    async def list_runs(args):  # type: ignore[no-untyped-def]
        return await _list_runs_handler(args)

    @tool(
        "get_run",
        (
            "Fetch a single scenario run by id with the full result blob "
            "(aggregates, projected decisions, per-tick state). Returns 404 "
            "for unknown or cross-tenant ids."
        ),
        {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Run id from run_scenario / list_runs."}
            },
            "required": ["run_id"],
        },
    )
    async def get_run(args):  # type: ignore[no-untyped-def]
        return await _get_run_handler(args)

    @tool(
        "list_projected_decisions",
        (
            "List projected decisions for a run — the per-anchor, per-persona "
            "verdicts the engine emitted. Use this when the user asks 'what "
            "did each persona decide' / 'show me the projections for run X' / "
            "'break down the renewal sim by anchor'. ``run_id`` is required; "
            "filter by ``anchor_id`` to drill into one decision. 404 "
            "(``foresight_run.not_found``) for unknown or cross-tenant ids. "
            "Returns ``{items[], total, limit, offset, has_more}``."
        ),
        {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Run id from run_scenario / list_runs.",
                },
                "anchor_id": {
                    "type": "string",
                    "description": (
                        "Optional filter on a single anchor (e.g. 'rollout:training'). "
                        "Omit to list across all anchors in the run."
                    ),
                },
                "limit": {"type": "integer", "description": "Max items (default 50, cap 500)."},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)."},
            },
            "required": ["run_id"],
        },
    )
    async def list_projected_decisions(args):  # type: ignore[no-untyped-def]
        return await _list_projected_decisions_handler(args)

    @tool(
        "get_aggregate",
        (
            "Workspace-level rolling accuracy + confidence drift + modal "
            "outcome distribution over the trailing window. Use this when the "
            "user asks 'how accurate were we' / 'did we predict X correctly' "
            "/ 'show me our hit rate'. Reads PredictionRecord docs across the "
            "whole workspace — not backtests; the dashboard's Backtest panel "
            "is a different surface. Empty workspaces return zeros + empty "
            "arrays (never 404). ``window_days`` defaults to 30 and caps at "
            "90 — above the cap surfaces ``foresight.invalid_window``."
        ),
        {
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "description": (
                        "Trailing window in days (default 30, cap 90). Omit for the default."
                    ),
                },
            },
            "required": [],
        },
    )
    async def get_aggregate(args):  # type: ignore[no-untyped-def]
        return await _get_aggregate_handler(args)

    @tool(
        "get_insights",
        (
            "Narrative insights synthesized over the workspace's recent "
            "PredictionRecords + backtests — accuracy drops, persona "
            "outliers, tier imbalances, threshold misses. Use this when the "
            "user asks 'what mattered in the last run' / 'explain the "
            "insights' / 'anything worth flagging'. Five-rule v0.5 "
            "synthesizer by default; LLM v1.0 synthesizer opt-in via "
            "workspace config (wire shape unchanged). Empty workspaces yield "
            "``items=[]`` — the synthesizer fires no rows when no patterns "
            "match. Surface ``severity`` (info | warning | critical) verbatim "
            "so the user sees the same colour the dashboard does. Response "
            "carries `synth_source` ('pattern' | 'llm') so the agent can "
            "disclose which synthesizer produced the rows."
        ),
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def get_insights(args):  # type: ignore[no-untyped-def]
        return await _get_insights_handler(args)

    @tool(
        "list_backtests",
        (
            "Trailing list of past backtests in the active workspace, most "
            "recent first. Use this when the user asks 'did we backtest yet' "
            "/ 'show me past backtests' / 'when was the last backtest'. "
            "Backtests are UI-initiated (ground-truth anchors anchor the "
            "scoring); this tool is READ-ONLY — to start a new backtest, "
            "redirect the user to the dashboard Aggregate panel. Returns "
            "``{items[]}`` (id, scenario_name, status, gate_decision, "
            "threshold, created_at) plus the ``limit`` / ``offset`` echo "
            "for pagination."
        ),
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max items (default 10, cap 200)."},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)."},
            },
            "required": [],
        },
    )
    async def list_backtests(args):  # type: ignore[no-untyped-def]
        return await _list_backtests_handler(args)

    @tool(
        "get_backtest",
        (
            "Fetch a single backtest by id with the full result blob, gate "
            "decision, and per-anchor calibration. Use this when the user "
            "asks 'what was the gate decision on backtest X' / 'show me the "
            "details of that backtest' / 'why did backtest X fail the gate'. "
            "Find ids via list_backtests. 404 "
            "(``foresight_backtest.not_found``) for unknown / malformed / "
            "cross-tenant ids. READ-ONLY — backtest creation ships through "
            "the dashboard."
        ),
        {
            "type": "object",
            "properties": {
                "backtest_id": {
                    "type": "string",
                    "description": "Id from list_backtests items.",
                }
            },
            "required": ["backtest_id"],
        },
    )
    async def get_backtest(args):  # type: ignore[no-untyped-def]
        return await _get_backtest_handler(args)

    @tool(
        "get_onboarding_gate",
        (
            "Workspace's foresight onboarding gate state — unlock status + "
            "reason + last backtest reference. Use when the user asks 'are "
            "we unlocked yet' / 'what's the gate' / 'why is foresight "
            "gated'. Empty workspaces return ``unlocked=False, "
            "reason='no_backtest'`` (not a 404) so the agent can explain "
            "why the gate is still closed. ``reason`` is one of "
            "``no_backtest`` | ``in_flight`` | ``below_threshold`` | "
            "``unlocked`` — surface it verbatim. READ-ONLY — to flip the "
            "gate, the user runs a backtest from the dashboard Aggregate "
            "panel."
        ),
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
    async def get_onboarding_gate(args):  # type: ignore[no-untyped-def]
        return await _get_onboarding_gate_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.2.0",
        tools=[
            list_scenarios,
            get_scenario,
            save_scenario,
            update_scenario,
            delete_scenario,
            run_scenario,
            list_runs,
            get_run,
            list_projected_decisions,
            get_aggregate,
            get_insights,
            list_backtests,
            get_backtest,
            get_onboarding_gate,
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "DELETE_SCENARIO_TOOL_ID",
    "FORESIGHT_TOOL_IDS",
    "GET_AGGREGATE_TOOL_ID",
    "GET_BACKTEST_TOOL_ID",
    "GET_INSIGHTS_TOOL_ID",
    "GET_ONBOARDING_GATE_TOOL_ID",
    "GET_RUN_TOOL_ID",
    "GET_SCENARIO_TOOL_ID",
    "LIST_BACKTESTS_TOOL_ID",
    "LIST_PROJECTED_DECISIONS_TOOL_ID",
    "LIST_RUNS_TOOL_ID",
    "LIST_SCENARIOS_TOOL_ID",
    "RUN_SCENARIO_TOOL_ID",
    "SAVE_SCENARIO_TOOL_ID",
    "SERVER_NAME",
    "UPDATE_SCENARIO_TOOL_ID",
    "build_foresight_server",
]
