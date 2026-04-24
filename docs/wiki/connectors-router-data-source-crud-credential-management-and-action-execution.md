---
{
  "title": "Connectors Router — Data Source CRUD, Credential Management, and Action Execution",
  "summary": "The connectors router provides a structured REST interface over PocketPaw's `ConnectorRegistry`, allowing the dashboard and enterprise UI to connect agents to external data sources (databases, APIs, cloud services) and execute actions against them. A structured status endpoint was added specifically to feed the `ConnectorCard` UI component with credential state and sync metadata.",
  "concepts": [
    "connectors",
    "ConnectorRegistry",
    "data source",
    "credential management",
    "action execution",
    "ConnectorCard",
    "ConnectorStatusResponse",
    "pocket-scoped credentials",
    "singleton registry",
    "enterprise integration"
  ],
  "categories": [
    "API",
    "Integration",
    "Data"
  ],
  "source_docs": [
    "66440d65557fe2fa"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Connectors in PocketPaw are typed integrations — each one wraps a specific external service (e.g., a SQL database, Google Drive, Notion) and exposes a set of actions the agent can execute. The connectors router sits between the REST API and the `ConnectorRegistry` singleton, providing a fully typed HTTP interface for the dashboard and enterprise UI panels.

## Registry as Singleton

The `_get_registry()` helper lazily initializes a single `ConnectorRegistry` instance:

```python
_registry: ConnectorRegistry | None = None

def _get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry
```

This avoids the cost of re-reading connector manifests from disk on every request. The singleton pattern is safe here because connector registrations are stable for the lifetime of a server process.

## Response Models as API Contract

The router defines its own Pydantic models (`ConnectorInfo`, `ConnectorActionInfo`, `ConnectorDetailResponse`, `ConnectorStatusResponse`) rather than passing registry internals directly to clients. This decouples the internal registry schema from the public API shape — the registry can evolve its data model without breaking clients as long as the router models remain stable.

## Connect and Disconnect Flow

`connect_connector` accepts credentials and calls into the registry's connect method. Credentials are never logged or stored in plaintext by the router layer; that responsibility belongs to the registry's credential store. `disconnect_connector` tears down the connection and clears stored credentials for the specified pocket.

The `_extras_key(pocket_id, connector_name)` helper builds a namespaced key for storing connector-specific metadata per pocket, preventing cross-pocket credential bleed.

## Action Execution

`execute_connector_action` takes an action name and arbitrary parameters, dispatches to the registry, and returns the result. The `ExecuteResponse` model wraps the output generically (`Any`), since connector action outputs are heterogeneous — a SQL query returns rows, a file connector returns file metadata, etc.

## Structured Status for `ConnectorCard` (PR2, 2026-04-19)

The `GET /connectors/{kind}/status` endpoint returns a `ConnectorStatusResponse` with four fields: `connected`, `last_sync`, `cred_state`, and `scope`. This was added as Gap C5 in the Feature Hardening Plan specifically because the enterprise `ConnectorCard` UI needed a machine-readable status payload rather than inferring state from a raw list response.

```python
class ConnectorStatusResponse(BaseModel):
    """Structured status for one connector in one pocket. Consumed by paw-enterprise's ConnectorCard."""
```

## Known Gaps

No TODOs in the source, but the connectors scope guard (`require_scope("connectors")`) is router-wide. Individual connectors may have different sensitivity levels — a read-only database connector arguably needs less privilege than one that can execute write actions. Granular per-connector scope checks are not currently enforced.