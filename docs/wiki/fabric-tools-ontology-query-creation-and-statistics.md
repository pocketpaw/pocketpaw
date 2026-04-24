---
{
  "title": "Fabric Tools: Ontology Query, Creation, and Statistics",
  "summary": "The Fabric tools module exposes three agent-facing tools — `FabricQueryTool`, `FabricCreateTool`, and `FabricStatsTool` — that allow the agent to interact with PocketPaw's Fabric ontology: a graph-based knowledge store for objects and their relationships. All three tools use lazy imports and silent telemetry to avoid circular dependencies and ensure tool calls never fail due to missing enterprise modules or sick tracing infrastructure.",
  "concepts": [
    "FabricQueryTool",
    "FabricCreateTool",
    "FabricStatsTool",
    "Fabric ontology",
    "graph store",
    "lazy import",
    "ee module",
    "BaseTool",
    "trace events",
    "SystemEvent",
    "message bus",
    "trust level"
  ],
  "categories": [
    "builtin tools",
    "ontology",
    "enterprise features",
    "graph database"
  ],
  "source_docs": [
    "bb31ce0fa8f975f4"
  ],
  "backlinks": null,
  "word_count": 572,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Fabric is PocketPaw's enterprise ontology layer — a graph store where typed objects (contacts, invoices, projects, etc.) are connected by named links. The `fabric_tools.py` module provides the agent-facing surface for that store through three `BaseTool` subclasses, created in March 2026 as part of the agent reasoning pipeline.

## Why Fabric exists

Without a structured graph store, an agent reasoning about cross-domain data ("which invoices are linked to this supplier?", "what projects does this contact own?") must rely on unstructured text retrieval. Fabric gives the agent a typed, queryable graph so it can traverse relationships directly rather than pattern-matching strings.

## FabricQueryTool

Tool name: `fabric_query`. Queries objects in the ontology by type, optionally filtered by a linked object and link type. Parameters include `type_name`, `linked_to`, `link_type`, and `limit`. Trust level is `high` — querying the graph is a read operation but exposes potentially sensitive structured data. The tool returns a formatted list of matching objects.

## FabricCreateTool

Tool name: `fabric_create`. Supports two `action` values:

- `create_object` — creates a new typed object with properties, optionally tagged with a source connector and source ID for provenance tracking.
- `create_link` — creates a directed link between two existing objects (`from_id` → `to_id`) with a named `link_type`.

The dual-action design avoids having two separate tools for object and link creation, keeping the agent's tool count lower. Trust level is `high`.

## FabricStatsTool

Tool name: `fabric_stats`. Returns aggregate statistics about the ontology (object counts by type, link counts, etc.). Useful for the agent to understand the size and shape of the knowledge graph before querying it. Trust level is `high`.

## Lazy import pattern

All three tools call `_get_fabric_store()`, which does a lazy import of `ee.api.get_fabric_store`:

```python
def _get_fabric_store():
    """Lazy import to avoid circular deps and missing ee/ module."""
    try:
        from ee.api import get_fabric_store
        return get_fabric_store()
    except ImportError:
        return None
```

This pattern serves two purposes:

1. **Circular dependency prevention**: `ee/` imports from core PocketPaw modules. If `fabric_tools.py` imported `ee` at module level, that would create a circular import that breaks the module graph at startup.
2. **Graceful degradation**: The `ee/` module is enterprise-only. On community installations where `ee/` is not installed, the import silently returns `None`, and each tool's `execute` method returns a user-friendly error instead of crashing.

## Trace event emission

`_emit_trace_events` publishes one `SystemEvent` per Fabric entry to the message bus so `TraceCollector` can aggregate them:

```python
async def _emit_trace_events(event_type: str, entries: list[dict[str, Any]]) -> None:
    """Publish one SystemEvent per entry so TraceCollector can aggregate them.
    Silent in the common case — the message bus only has subscribers when a
    proposal is actively being traced. Any failure is swallowed so tool calls
    never break because telemetry is sick.
    """
```

The `try/except` that swallows all exceptions is intentional: telemetry is observability infrastructure, not business logic. If the bus is unavailable, the tool call must still succeed — tracing failure must never propagate to the user.

## Known Gaps

- **No `update_object` action**: `FabricCreateTool` can create objects and links but cannot update properties on an existing object. This forces callers to delete and recreate to modify data.
- **No delete operations**: There is no `FabricDeleteTool` or `delete` action. Objects and links in the graph cannot be removed via agent tools.
- **ee/ dependency not documented**: The dependency on the enterprise `ee/` module is implicit. A community user who calls `fabric_query` receives a generic error with no indication that the `ee/` package is required.