---
{
  "title": "Tools Registry REST Endpoint",
  "summary": "Provides a single GET endpoint that aggregates builtin tools, MCP tools, and tool groups into a unified discovery response. Lazy imports and broad exception handling ensure the endpoint returns partial results gracefully when the MCP manager or OAuth token store is not yet initialized.",
  "concepts": [
    "tools registry",
    "builtin tools",
    "MCP tools",
    "tool groups",
    "trust level",
    "OAuth status",
    "TOOL_GROUPS",
    "lazy import",
    "FastAPI router",
    "tool discovery"
  ],
  "categories": [
    "api",
    "tools",
    "mcp",
    "integrations"
  ],
  "source_docs": [
    "215052f2abce4f3d"
  ],
  "backlinks": null,
  "word_count": 471,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Tools Registry REST Endpoint

The tools router exposes a single `GET /tools` endpoint that serves as a runtime registry view — a snapshot of every capability PocketPaw can invoke at the moment of the request. It aggregates three distinct sources: the builtin tool registry, the MCP server tool catalog, and the OAuth connection status for external service integrations.

### Why a Unified Endpoint

Without a unified discovery endpoint, a dashboard or orchestrator would need to query three separate subsystems, handle partial failures from each, and merge the results client-side. The tools endpoint centralizes this aggregation on the server, returning a consistent shape even when one or more subsystems are unavailable.

### Builtin Tools

Builtin tools are read from `pocketpaw.tools.cli._TOOLS`, a module-level dict populated at startup. The import is deferred inside the function body to avoid circular import problems at module load time — the tools module imports from other subsystems that may not be fully initialized when the router is registered. The result is sorted alphabetically by name so the dashboard can render a predictable list without client-side sorting.

Each tool entry exposes `name`, `description`, and `trust_level`. The trust level distinguishes tools that require explicit user approval (`high_trust`) from those that can run autonomously (`low_trust`). Surfacing this in the registry allows clients to display appropriate confirmation UI before invoking sensitive tools.

### MCP Tools

MCP (Model Context Protocol) tools come from external servers connected to PocketPaw. The MCP manager may not be initialized if no MCP servers are configured, so the retrieval is wrapped in a broad `except Exception` with debug-level logging. This is an intentional resilience pattern: MCP availability is optional, and its absence should not cause the entire tools endpoint to fail. Returning an empty `mcp_tools` list allows the dashboard to render "No MCP servers connected" rather than an error state.

### OAuth Connection Status

The endpoint also collects OAuth token status for Google services and Spotify. For each service, it checks whether a saved access token exists in the `TokenStore`. The distinction between `"connected"`, `"not_configured"`, and (implicitly) unconfigured informs the dashboard about which integrations are ready to use without requiring the user to navigate to a settings page.

### Tool Groups

The `TOOL_GROUPS` dict from `pocketpaw.tools.policy` maps group names (e.g., `group:fs`, `group:web`) to lists of tool names. Groups are the policy primitive for granting bulk tool permissions — granting `group:fs` gives access to all filesystem tools simultaneously. Exposing groups in the registry allows permission management UIs to present logical bundles rather than individual tool checkboxes.

### Known Gaps

MCP tools are reported with a hardcoded `"status": "connected"` rather than reflecting the actual connection health. A server that was connected at startup but has since become unreachable would still appear as connected. Adding real-time health checking (or at least a `last_seen` timestamp) would make this field more actionable.