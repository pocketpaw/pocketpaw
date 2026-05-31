"""In-process MCP servers exposed to agent backends for cloud features.

Each module here builds a Claude Agent SDK ``create_sdk_mcp_server`` and is
surfaced to core via a ``pocketpaw.mcp_servers`` ``McpServerProvider`` entry-
point (see ``pocketpaw_ee.extensions``). Core discovers them through
``pocketpaw._registry`` and never imports this package directly — the OSS-EE
boundary.

Moved here from ``src/pocketpaw/agents/sdk_mcp_*.py`` in the OSS-EE split
(Phase 3b): these servers are cloud-only (tasks, planner, pocket context),
so they belong in ``pocketpaw_ee``. The ripple widget-spec tools, which have
no cloud dependency, stayed in core as ``pocketpaw.agents.sdk_mcp_widgets``.
"""
