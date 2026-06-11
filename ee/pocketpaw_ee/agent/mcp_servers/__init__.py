"""In-process MCP servers exposed to agent backends for cloud features.

Updated: 2026-06-11 (feat/fabric-instinct-mcp-providers) — added the server
surface listing below, plus the two new read-only servers (``fabric.py`` /
``instinct.py``). On the claude_agent_sdk backend, registry tools (BaseTool)
never reach the agent — only MCP servers do — so anything the cloud chat agent
must touch needs a server here.

Each module here builds a Claude Agent SDK ``create_sdk_mcp_server`` and is
surfaced to core via a ``pocketpaw.mcp_servers`` ``McpServerProvider`` entry-
point (see ``pocketpaw_ee.extensions``). Core discovers them through
``pocketpaw._registry`` and never imports this package directly — the OSS-EE
boundary.

Server surfaces (module → server name → tools):

* ``belt.py`` → ``pocketpaw_belt`` → ``belt_propose_change`` (gated code change)
* ``connectors.py`` → ``pocketpaw_connectors`` → connector listing + execution
* ``decisions.py`` → ``pocketpaw_decisions`` → Decision-Graph queries
* ``external_actions.py`` → ``pocketpaw_external_actions`` →
  ``propose_external_action`` (gated connector call — the propose path for
  this backend)
* ``fabric.py`` → ``pocketpaw_fabric`` → ``fabric_query`` / ``fabric_stats``
  (READ-ONLY ontology access, workspace-scoped)
* ``foresight.py`` → ``pocketpaw_foresight`` → scenario save/run
* ``instinct.py`` → ``pocketpaw_instinct`` → ``instinct_pending`` /
  ``instinct_audit`` (READ-ONLY gate visibility, workspace-scoped; proposing
  goes through ``pocketpaw_external_actions``)
* ``loom.py`` → ``loom`` (stdio config) → codebase orientation reads
* ``media.py`` → ``pocketpaw_media`` → ``image_generate`` / ``video_generate``
* ``meetings.py`` → ``pocketpaw_meetings`` → meeting queries
* ``planner.py`` → ``pocketpaw_planner`` (opt-in) + ``pocketpaw_pocket_planner``
* ``pockets.py`` → ``pocketpaw_pocket`` → pocket context + widget pinning
* ``sites.py`` / ``sites_create.py`` → ``pocketpaw_sites_manager`` → Paw Sites
* ``tasks.py`` → ``pocketpaw_tasks`` → task CRUD

Moved here from ``src/pocketpaw/agents/sdk_mcp_*.py`` in the OSS-EE split
(Phase 3b): these servers are cloud-only (tasks, planner, pocket context),
so they belong in ``pocketpaw_ee``. The ripple widget-spec tools, which have
no cloud dependency, stayed in core as ``pocketpaw.agents.sdk_mcp_widgets``.
"""
