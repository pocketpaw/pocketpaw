# pulley.py — registers the pulley block-engine MCP server for the BELT surface
# (the develop station). Created: 2026-09-12 (A1, belt factory).
#
# What this file does: the same Path A trick as its sibling ``loom.py`` — a
# stdio config pass-through, NOT an in-process SDK server. pulley is a finished
# external TypeScript server that already speaks MCP over stdio
# (``bun <pulley>/mcp/server.ts``, server name "pulley", 5 tools:
# search_catalog / describe_block / plan_install / apply_plan / doctor). Rather
# than re-implement those tools, this module returns the McpStdioServerConfig
# dict the claude_agent_sdk's ``mcp_servers`` option accepts natively; the
# pocketpaw registration loop assigns it through untouched.
#
# Why the station wants it: without pulley the develop-station agent hand-writes
# auth, org, roles, notify, files and audit code for every client app. With it,
# it installs reviewed blocks — and the plan/apply split keeps the human gate:
# ``plan_install`` writes nothing and every refusal happens there, ``apply_plan``
# re-plans against current disk and only writes when the fresh plan matches.
#
# Launch line (verified against pulley/mcp/server.ts ``parseArgs`` and
# pulley/mcp/lib/belt.ts): ``bun <pulley_path>/mcp/server.ts``. Two flags the
# server accepts and this module deliberately does NOT pass:
#
#   ``--registry`` — BeltRunner defaults ``pulleyRoot`` to the server file's own
#   checkout and the registry falls back to ``<pulley-root>/registry``, both
#   derived from the absolute server.ts path already passed. A setting for it
#   would buy no behaviour.
#
#   ``--app`` — the client repo blocks install INTO. Omitting it is the per-run
#   scoping, not a gap in it. ``PulleyTools.definitions`` builds the schema from
#   the running deployment (``appRequired = this.defaultApp ? [] : ["app"]``), so
#   a server started WITHOUT a default app advertises ``app`` as REQUIRED on
#   every tool, and ``#app()`` returns a usage error if a call omits it. A belt
#   run develops in a per-run station worktree and proposes the diff of THAT
#   repo, so a fixed default would install blocks into a directory the station's
#   diff never sees — silently, since the tools would still succeed. Making the
#   agent name the repo per call is what keeps the two in step; the preamble
#   tells it to pass the same repo it hands ``belt_propose_change``.
# (A1 shipped a ``pulley_app_path`` setting for this; A1b removed it.)
#
# Graceful-by-default: ``build_pulley_server`` returns None — never raising —
# when ``pulley_path`` is unset, the server.ts is missing, or bun cannot be
# resolved. A None return means the registration loop simply skips it; chat
# keeps working with block installs simply absent.
"""Agent-side registration of the pulley block-engine MCP server."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The pulley server names itself "pulley" (serverInfo.name in its initialize
# response), so the namespace and tool ids key on it.
SERVER_NAME = "pulley"

# Claude Code namespaces MCP tools as ``mcp__<server>__<tool>``. Allowlist
# entries must use this exact form. pulley exposes these 5 tools — four reads
# and ``apply_plan``, the only one that writes.
SEARCH_CATALOG_TOOL_ID = f"mcp__{SERVER_NAME}__search_catalog"
DESCRIBE_BLOCK_TOOL_ID = f"mcp__{SERVER_NAME}__describe_block"
PLAN_INSTALL_TOOL_ID = f"mcp__{SERVER_NAME}__plan_install"
APPLY_PLAN_TOOL_ID = f"mcp__{SERVER_NAME}__apply_plan"
DOCTOR_TOOL_ID = f"mcp__{SERVER_NAME}__doctor"

PULLEY_TOOL_IDS = (
    SEARCH_CATALOG_TOOL_ID,
    DESCRIBE_BLOCK_TOOL_ID,
    PLAN_INSTALL_TOOL_ID,
    APPLY_PLAN_TOOL_ID,
    DOCTOR_TOOL_ID,
)


def _resolve_bun_bin(pulley_bin: str) -> str | None:
    """Resolve the bun binary path, mirroring loom's 3-step discovery order.

    Order: an explicit absolute/relative path that exists → a PATH lookup of the
    given name → the ~/.bun/bin/bun fallback (bun's own installer location).
    Returns the resolved path, or None when nothing on disk matches (so the
    caller can degrade to None).
    """
    # 1. Explicit path that points at a real executable file.
    candidate = Path(pulley_bin).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)

    # 2. PATH lookup (handles the bare-name default "bun").
    on_path = shutil.which(pulley_bin)
    if on_path:
        return on_path

    # 3. ~/.bun/bin/bun fallback (where bun's install script puts it).
    bun_home = Path.home() / ".bun" / "bin" / "bun"
    if bun_home.is_file() and os.access(bun_home, os.X_OK):
        return str(bun_home)

    return None


def build_pulley_server() -> tuple[str, Any] | None:
    """Build the pulley stdio MCP server config, or None when it's unavailable.

    Returns ``(SERVER_NAME, <McpStdioServerConfig dict>)`` when the pulley
    checkout is configured and bun resolves on disk. Returns None — never
    raising — when:
      * ``pulley_path`` is unset (pulley disabled by default), or
      * ``<pulley_path>/mcp/server.ts`` does not exist, or
      * the bun binary cannot be resolved.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()

    pulley_path = settings.pulley_path
    if not pulley_path:
        logger.debug("pulley MCP server disabled — pulley_path is unset")
        return None

    server_ts = Path(pulley_path).expanduser() / "mcp" / "server.ts"
    if not server_ts.is_file():
        logger.warning("pulley MCP server not registered — server not found at %s", server_ts)
        return None

    bun_bin = _resolve_bun_bin(settings.pulley_bin)
    if bun_bin is None:
        logger.warning(
            "pulley MCP server not registered — bun binary %r not found on PATH or at "
            "~/.bun/bin/bun",
            settings.pulley_bin,
        )
        return None

    # No ``--app``: the omission IS the per-run scoping. See the module docstring.
    config: dict[str, Any] = {"type": "stdio", "command": bun_bin, "args": [str(server_ts)]}
    logger.info("pulley MCP server registered — server %s, app per call", server_ts)
    return SERVER_NAME, config
