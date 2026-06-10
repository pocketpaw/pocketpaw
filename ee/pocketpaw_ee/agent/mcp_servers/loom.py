# loom.py — registers the loom codebase-orientation binary as an MCP server for
# the claude_agent_sdk cloud chat backend. Created: 2026-06-10 (BS-1, Belt &
# Pulley stations thin slice).
#
# What this file does (Path A — stdio config pass-through, NOT an in-process SDK
# server): loom is a finished external Go binary that already speaks MCP over
# stdio (`loom mcp -model <worldmodel.json>`, server name "loom", 5 read tools:
# orient / locate / why / what_depends_on / boundaries). Rather than re-implement
# those tools as an in-process SDK proxy, this module returns a stdio server
# CONFIG DICT — the McpStdioServerConfig shape the claude_agent_sdk's
# ``mcp_servers`` option accepts natively ({"type":"stdio","command":...,
# "args":[...]}). The pocketpaw registration loop in
# ``pocketpaw.agents.claude_sdk._get_mcp_servers`` does ``servers[name] =
# cfg_entry`` with NO transformation (claude_sdk.py:738/751), and the existing
# file-based stdio path already builds the same dict shape (claude_sdk.py:659),
# so a config dict flows through to ``ClaudeAgentOptions.mcp_servers`` exactly
# like an SDK server object would. No loop change was needed — see the PR body
# spike for the file:line evidence.
#
# Tool ids namespace as ``mcp__loom__orient`` etc. (the binary's own server
# name is "loom"), so the Claude Code allowlist machinery matches them.
#
# Graceful-by-default: ``build_loom_server`` returns None — never raising — when
#   * ``loom_model_path`` is unset (loom disabled), or
#   * the loom binary cannot be resolved on disk.
# A None return means the entry-point loop simply skips registration; chat keeps
# working without orientation. Binary resolution mirrors loom's own discovery
# order: explicit ``loom_bin`` setting → PATH lookup → ~/go/bin/loom fallback.
"""Agent-side registration of the loom codebase-orientation MCP server."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The loom binary names its own MCP server "loom" (serverInfo.name in the
# initialize response), so the in-process namespace and tool ids key on it.
SERVER_NAME = "loom"

# Claude Code namespaces MCP tools as ``mcp__<server>__<tool>``. Allowlist
# entries must use this exact form. loom exposes these 5 read tools.
ORIENT_TOOL_ID = f"mcp__{SERVER_NAME}__orient"
LOCATE_TOOL_ID = f"mcp__{SERVER_NAME}__locate"
WHY_TOOL_ID = f"mcp__{SERVER_NAME}__why"
WHAT_DEPENDS_ON_TOOL_ID = f"mcp__{SERVER_NAME}__what_depends_on"
BOUNDARIES_TOOL_ID = f"mcp__{SERVER_NAME}__boundaries"

LOOM_TOOL_IDS = (
    ORIENT_TOOL_ID,
    LOCATE_TOOL_ID,
    WHY_TOOL_ID,
    WHAT_DEPENDS_ON_TOOL_ID,
    BOUNDARIES_TOOL_ID,
)


def _resolve_loom_bin(loom_bin: str) -> str | None:
    """Resolve the loom binary path, mirroring loom's own discovery order.

    Order: an explicit absolute/relative path that exists → a PATH lookup of the
    given name → the ~/go/bin/loom fallback. Returns the resolved absolute path,
    or None when nothing on disk matches (so the caller can degrade to None).
    """
    # 1. Explicit path that points at a real executable file.
    candidate = Path(loom_bin).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)

    # 2. PATH lookup (handles the bare-name default "loom").
    on_path = shutil.which(loom_bin)
    if on_path:
        return on_path

    # 3. ~/go/bin/loom fallback (the conventional Go install location).
    go_bin = Path.home() / "go" / "bin" / "loom"
    if go_bin.is_file() and os.access(go_bin, os.X_OK):
        return str(go_bin)

    return None


def build_loom_server() -> tuple[str, Any] | None:
    """Build the loom stdio MCP server config, or None when loom is unavailable.

    Returns ``(SERVER_NAME, <McpStdioServerConfig dict>)`` when both the
    world-model path is set in settings and the loom binary resolves on disk.
    Returns None — never raising — when:
      * ``loom_model_path`` is unset (loom disabled by default), or
      * the configured world-model file does not exist, or
      * the loom binary cannot be resolved.

    The returned dict is the McpStdioServerConfig shape the claude_agent_sdk
    consumes natively: ``loom mcp -model <model path>`` served over stdio.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()

    model_path = settings.loom_model_path
    if not model_path:
        logger.debug("loom MCP server disabled — loom_model_path is unset")
        return None

    model_file = Path(model_path).expanduser()
    if not model_file.is_file():
        logger.warning(
            "loom MCP server not registered — world-model not found at %s", model_file
        )
        return None

    loom_bin = _resolve_loom_bin(settings.loom_bin)
    if loom_bin is None:
        logger.warning(
            "loom MCP server not registered — loom binary %r not found on PATH "
            "or at ~/go/bin/loom",
            settings.loom_bin,
        )
        return None

    config: dict[str, Any] = {
        "type": "stdio",
        "command": loom_bin,
        "args": ["mcp", "-model", str(model_file)],
    }
    logger.info("loom MCP server registered — model %s", model_file)
    return SERVER_NAME, config
