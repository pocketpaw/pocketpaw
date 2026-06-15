# tool_executor.py — Server-side executor for pocket tool invocations.
# Created: 2026-05-24 (#1206 part a — invoke_tool wire).
# Updated: 2026-06-15 (feat/invoke-tool-v1) — UNLOCKED. The executor now
#   dispatches a connector-action grant ("connector:<name>:<action>") through
#   the already-shipped `connectors.service.execute` path (#1376), reusing it
#   rather than building a second connector engine. v1 is READ-FIRST, mirroring
#   the chat MCP `connector_execute` gate order EXACTLY:
#     Gate 1 — the connector must be bound to THIS pocket / workspace
#              (`is_connector_bound_to_pocket`). execute() is trust-agnostic and
#              does NOT enforce trust, so the gate order below is load-bearing.
#     Gate 2 — look up the action's trust (`get_action_trust`); unknown → error.
#     Gate 3 — READ/WRITE split. A WRITE action (trust.is_read False) NEVER
#              reaches execute() in v1 — it returns code="blocked" (the
#              "coming in v2 (Instinct)" message). A v2 seam is marked below.
#     Gate 4 — a READ action fires execute() and returns its result.
#   The `get_pocket_allowed_tools` `[]` stub is RETIRED: the router now reads
#   `allowed_tools` off the credential row (via `get_pocket_backend_for_executor`)
#   and passes the tool NAMES by parameter. The `not_allowed` gate is unchanged.
#
# IMPORT-LINTER: must NOT import `pocketpaw_ee.cloud.models.*`. The executor
#   receives the allowlist (a list of tool-name strings) BY PARAMETER only —
#   the router / service owns Beanie access. Lazy imports of `connectors.service`
#   / `connectors.dto` / `_core.errors` inside `run_tool` keep the module's
#   import surface clean (the same pattern the chat MCP handler uses).

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# v1 write refusal — kept as a constant so the test pins the exact contract.
# Mirrors the chat MCP layer's `_V2_BLOCK_MESSAGE` posture so the two surfaces
# (chat `connector_execute` and click-driven `invoke_tool`) stay consistent.
_WRITE_BLOCK_MESSAGE = "write actions need approval — coming in v2 (Instinct)"


async def run_tool(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    tool: str,
    args: dict[str, Any],
    allowed_tools: list[str],
) -> dict[str, Any]:
    """Run a named tool with the resolved args, returning a wire dict shaped
    like :class:`RunToolResponse` (``{ok, tool, status, response, error, code}``).

    Gate order (mirrors the chat MCP ``connector_execute`` handler):

    * ``tool not in allowed_tools`` → ``code="not_allowed"`` (fail-closed; the
      allowlist is read off the per-pocket backend row by the router and passed
      in by parameter, NEVER from the spec).
    * a ``connector:<name>:<action>`` grant dispatches through
      ``connectors.service.execute`` after Gates 1–3 below.
    * any other (built-in/registry) grant is not wired in v1 →
      ``code="unknown_tool"``.

    ``workspace_id`` / ``user_id`` are threaded for the audit log + a future
    per-(pocket, user) rate limit.
    """
    if tool not in allowed_tools:
        logger.info(
            "tool_executor.run_tool denied: tool=%r pocket=%r user=%r reason=not_allowed",
            tool,
            pocket_id,
            user_id,
        )
        return {
            "ok": False,
            "tool": tool,
            "error": f"tool {tool!r} is not on the pocket's allowlist",
            "code": "not_allowed",
        }

    # --- connector-action grant: "connector:<name>:<action>" ----------------
    if tool.startswith("connector:"):
        return await _run_connector_tool(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            tool=tool,
            args=args,
        )

    # --- built-in / registry tool grant (web_fetch, etc.) -------------------
    # v1 scope: connector-action grants ship first. A built-in registry
    # dispatch (WebFetch / Composio) is a thin follow-up that routes `tool` +
    # `args` to the existing tool registry and flattens into the SAME wire
    # dict. A URL-taking built-in (e.g. a future `web_fetch`) MUST route its
    # outbound fetch through `_http_guard` (the SSRF boundary the source /
    # action executors use) when that path lands. Until then:
    logger.info(
        "tool_executor.run_tool unknown: tool=%r pocket=%r user=%r — registry not wired",
        tool,
        pocket_id,
        user_id,
    )
    return {
        "ok": False,
        "tool": tool,
        "error": f"tool {tool!r} has no registry implementation wired yet",
        "code": "unknown_tool",
    }


async def _run_connector_tool(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a ``connector:<name>:<action>`` grant.

    Reuses ``connectors.service.execute`` (the single CLOUD-path connector
    executor, #1376) for READ actions. Because ``execute`` is TRUST-AGNOSTIC,
    this function enforces the three gates the executor does NOT: bind/tenancy
    (Gate 1), trust lookup (Gate 2), and the READ/WRITE split (Gate 3). A WRITE
    never reaches ``execute`` in v1.
    """
    # Lazy imports — keep the module import-linter-clean (no static
    # connectors / errors dependency at import time), matching the chat MCP
    # handler's pattern.
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    try:
        _, conn_name, action = tool.split(":", 2)
    except ValueError:
        return {
            "ok": False,
            "tool": tool,
            "error": f"malformed connector grant {tool!r} — expected connector:<name>:<action>",
            "code": "bad_grant",
        }
    if not conn_name or not action:
        return {
            "ok": False,
            "tool": tool,
            "error": f"malformed connector grant {tool!r} — empty connector name or action",
            "code": "bad_grant",
        }

    # Gate 1 — reachability / tenancy. `connectors.service.execute` re-checks
    # this internally (service.py is_connector_bound_to_pocket on the pocket
    # arm), but we ALSO check it here for defense-in-depth: it is the tenant
    # boundary and the executor must never assume the caller pre-verified it.
    # An agent in pocket A must not reach a connector bound only to pocket B.
    bound = await connectors_service.is_connector_bound_to_pocket(
        workspace_id, pocket_id, conn_name
    )
    if not bound:
        logger.info(
            "tool_executor.run_tool not_reachable: tool=%r pocket=%r connector=%r",
            tool,
            pocket_id,
            conn_name,
        )
        return {
            "ok": False,
            "tool": tool,
            "error": f"connector {conn_name!r} is not reachable from this pocket",
            "code": "not_reachable",
        }

    # Gate 2 — trust lookup. Unknown action → clear error (never execute).
    trust = await connectors_service.get_action_trust(conn_name, action)
    if trust is None:
        return {
            "ok": False,
            "tool": tool,
            "error": f"connector {conn_name!r} has no action {action!r}",
            "code": "unknown_tool",
        }

    # Gate 3 — READ/WRITE split. THE load-bearing security rule: a WRITE action
    # NEVER calls execute() in v1. It returns a success-shaped blocked response
    # so the home-grid `on_success` handler can branch on `code == "blocked"`,
    # mirroring the chat MCP `_V2_BLOCK_MESSAGE` posture.
    #
    # v2 SEAM: replace this refusal with a call to
    #   pocketpaw_ee.cloud.external_actions.propose.propose_external_action(...)
    # which files a pending Instinct Action (propose → human approves →
    # execute_approved_external_action re-enters connectors.service.execute).
    # Return {ok:true, status:202, code:"instinct_pending",
    #         response:{proposed_action_id, status:"pending_approval"}}.
    # DO NOT build v2 here — v1 refuses.
    if not trust.is_read:
        logger.info(
            "tool_executor.run_tool blocked write: tool=%r pocket=%r connector=%r action=%r",
            tool,
            pocket_id,
            conn_name,
            action,
        )
        return {
            "ok": False,
            "tool": tool,
            "status": 200,
            "code": "blocked",
            "error": _WRITE_BLOCK_MESSAGE,
            "response": {
                "executed": False,
                "blocked": True,
                "connector": conn_name,
                "action": action,
                "reason": _WRITE_BLOCK_MESSAGE,
            },
        }

    # Gate 4 — READ fires now. Build the SAME ExecuteActionRequest the chat MCP
    # `connector_execute` builds, so both callers converge on one execute path.
    body = ExecuteActionRequest(
        action=action,
        params=args,
        scope="pocket" if pocket_id else "workspace",
        pocket_id=pocket_id or None,
    )
    try:
        result = await connectors_service.execute(
            workspace_id, conn_name, body, user_id=user_id
        )
    except CloudError as exc:
        return {
            "ok": False,
            "tool": tool,
            "status": exc.status_code,
            "error": exc.message,
            "code": exc.code,
        }

    logger.info(
        "tool_executor.run_tool fired read: tool=%r pocket=%r connector=%r action=%r success=%s",
        tool,
        pocket_id,
        conn_name,
        action,
        result.success,
    )
    return {
        "ok": result.success,
        "tool": tool,
        "status": 200 if result.success else 502,
        "response": result.data,
        "error": result.error,
    }
