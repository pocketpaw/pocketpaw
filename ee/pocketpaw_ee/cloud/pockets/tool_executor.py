# tool_executor.py — Server-side executor for pocket tool invocations.
# Created: 2026-05-24 (#1206 part a — invoke_tool wire).
#
# The click-driven sibling of ``source_executor`` (read-only fetch) and
# ``action_executor`` (named write binding). This executor receives a tool
# NAME plus pre-resolved args from the ``invoke_tool`` ripple action verb
# and runs the named tool against the pocket's tool allowlist.
#
# In part (a) the allowlist is intentionally empty — the wire is locked
# down before any tool can fire. The real tool registry + Composio /
# WebFetch wiring lands in a follow-up; part (b) adds the home-grid
# ``onEvent`` plumbing that POSTs to ``/pockets/{id}/tools/run`` and the
# allowlist storage on the per-pocket backend config.
#
# IMPORT-LINTER: must NOT import ``pocketpaw_ee.cloud.models.*``. The
# executor receives the allowlist by parameter only — the router /
# service owns Beanie access.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def get_pocket_allowed_tools(workspace_id: str, pocket_id: str) -> list[str]:
    """Return the list of tool names allowed for this pocket.

    Part (a) intentionally returns ``[]`` for every pocket — the wire is
    in place but no tool is enabled until the captain explicitly turns
    one on per pocket (the allowlist storage lives on the per-pocket
    backend config row in a follow-up). The empty default is fail-closed:
    every ``invoke_tool`` POST is rejected with ``code="not_allowed"``
    until that follow-up ships.
    """
    # `workspace_id` and `pocket_id` are accepted now so callers don't
    # have to change shape when the real allowlist is plumbed through.
    _ = (workspace_id, pocket_id)
    return []


async def run_tool(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    tool: str,
    args: dict[str, Any],
    allowed_tools: list[str],
) -> dict[str, Any]:
    """Run a named tool with the resolved args, returning a wire dict
    shaped like :class:`RunActionResponse`.

    Part (a) wires only the gate: an empty allowlist (default) rejects
    every call with ``code="not_allowed"``; a tool name not on the
    allowlist gets the same response. Real tool execution (Composio /
    WebFetch routing through the existing tool registry) lands in a
    follow-up — the response shape stays the same so the home-grid
    reconcile handlers don't change.

    ``workspace_id`` and ``user_id`` are accepted now so the future audit
    log + per-(pocket, user) rate limit can plumb in without touching the
    route signature.
    """
    # The kwargs are part of the public contract — accept and pass-through
    # even before the real executor lands so the route signature doesn't
    # have to change later.
    _ = (workspace_id, user_id, args)

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

    # The allowlist passed the name through, but no tool registry is
    # wired up yet in part (a). Surface that as a structured error so
    # the home-grid reconcile handlers can show a friendly toast; the
    # follow-up replaces this branch with the real tool dispatch.
    logger.info(
        "tool_executor.run_tool unknown: tool=%r pocket=%r user=%r — registry not wired",
        tool,
        pocket_id,
        user_id,
    )
    return {
        "ok": False,
        "tool": tool,
        "error": f"tool {tool!r} is allowlisted but no registry implementation is wired yet",
        "code": "unknown_tool",
    }
