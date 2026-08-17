# Shared tool-call narration for every cloud surface.
# Created: 2026-08-15 (HTN-11) — lifted verbatim out of
# ``agent_bridge._narrate_tool_use`` so the streaming ``run_core`` path can call
# the SAME function rather than grow a second phrasing implementation.
#
# Why this module exists at all: narration shipped in HTN-1 wired to exactly one
# surface, the group/DM WebSocket bridge. The streaming chat surface yields its
# own ``tool_start`` frame from ``run_core`` and never learned about the field,
# so the main chat showed raw tool names while group/DM read as English. The
# asymmetry was invisible because each path had its own tests and neither
# compared against the other.
#
# This mirrors ``plan_normalizer``: one module, both transports, so a phrasing
# rule can never be true on one surface and false on the other. A caller that
# wants the phrase imports ``narrate_tool_use``; nobody re-implements it.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def narrate_tool_use(tool_name: str, tool_input: Any = None, instance: Any = None) -> str | None:
    """Render a plain-language phrase for a tool call, or None.

    Returns None for any tool that has no phrasing at all — narration is
    decoration, so it must never raise into, or block, the response stream.

    ``instance`` is the running agent, and it is what makes a tool's OWN
    declared phrase reachable: ``narration_registry_for`` resolves the live
    ``ToolRegistry`` backing that agent's bridged tool surface, and the lookup
    reads the ``Narration`` off the instance the registry already holds. It is
    never constructed — ``ShellTool.__init__`` calls ``get_settings()``, and on
    cloud EE substitutes ``DaytonaShellTool`` under the same name, so building a
    tool to read a property would run real setup on the event loop to phrase a
    status line, and could phrase the wrong tool's.

    Without a resolvable registry (the Claude SDK backend surfaces its tools
    over MCP rather than through the bridge) the lookup still answers from the
    override table or by deriving from the tool name — that is why the cloud
    path's ``litellm_web_search`` narrates with its query either way, and why
    the registry is what rescues the builtin ``web_search`` specifically.

    A non-dict ``tool_input`` is coerced to ``{}`` rather than passed through.
    Both callers type it ``Any`` (it is whatever the backend put in
    ``event.metadata["input"]``), and ``render`` is declared against ``dict |
    None``. Without the coercion a string there raises inside ``render`` and the
    except below swallows it into ``None`` — no phrase at all. Coerced, the
    lookup still yields the BARE phrase, which is the documented fallback: an
    unknown argument means "don't interpolate", not "say nothing".
    """
    if not tool_name:
        return None
    if not isinstance(tool_input, dict):
        tool_input = {}
    try:
        from pocketpaw.tools.narration import narration_for_tool, render

        registry = None
        if instance is not None:
            from pocketpaw.agents.tool_bridge import narration_registry_for

            registry = narration_registry_for(getattr(instance, "backend", None))
        return render(narration_for_tool(tool_name, registry), tool_input)
    except Exception:
        logger.debug("Tool narration failed for %s", tool_name, exc_info=True)
        return None
