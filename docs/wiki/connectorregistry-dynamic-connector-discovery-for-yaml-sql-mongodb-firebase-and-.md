---
{
  "title": "ConnectorRegistry: Dynamic Connector Discovery for YAML, SQL, MongoDB, Firebase, and GCP Adapters",
  "summary": "`ConnectorRegistry` scans a directory for YAML connector definitions and also hard-registers native Python adapters (SQL databases, MongoDB, Firebase, GCP) that cannot be expressed as simple REST YAML. It manages per-pocket adapter instances and provides a `reload()` method for hot configuration changes.",
  "concepts": [
    "ConnectorRegistry",
    "YAML connector",
    "native adapter",
    "per-pocket isolation",
    "hot reload",
    "AnyAdapter",
    "runtime_checkable",
    "MongoDBAdapter",
    "FirebaseAdapter",
    "GCPAdapter"
  ],
  "categories": [
    "connectors",
    "architecture",
    "registry"
  ],
  "source_docs": [
    "b9b17df0cda448d4"
  ],
  "backlinks": null,
  "word_count": 392,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ConnectorRegistry` is the central service that makes connectors available to the agent. It bridges two kinds of adapters: YAML-defined REST connectors (most third-party APIs) and native Python connectors (databases, CLI tools) that require driver-level access.

## Two-Track Discovery

```python
_SQL_CONNECTORS: set[str] = {"postgresql", "mysql", "mssql", "sqlite"}
_NATIVE_CONNECTORS: dict[str, type] = {
    "mongodb": MongoDBAdapter,
    "firebase": FirebaseAdapter,
    "gcp": GCPAdapter,
}
```

On startup, `_scan()`:
1. Walks the `connectors_dir` for `*.yaml` files and parses each via `parse_connector_yaml`.
2. Registers hard-coded native adapter classes for connectors that cannot be described in YAML (databases need driver connections, CLI adapters need subprocess management).

This avoids requiring a YAML file for every connector — native adapters are registered by name and instantiated on demand.

## Per-Pocket Isolation

```python
self._instances: dict[str, dict[str, AnyAdapter]] = {}
# {pocket_id: {connector_name: adapter_instance}}
```

Each pocket gets its own adapter instance map. This is important because adapter instances hold connection state (active client, credentials, database handle). Sharing an instance across pockets would mix credentials and connection pools.

## `AnyAdapter` Protocol

```python
@runtime_checkable
class AnyAdapter(Protocol):
    name: str
    display_name: str
    async def connect(...) -> ConnectionResult: ...
    async def disconnect(...) -> bool: ...
    async def actions(...) -> list[ActionSchema]: ...
    async def execute(...) -> ActionResult: ...
```

`runtime_checkable` allows `isinstance(obj, AnyAdapter)` checks at runtime, which the registry uses to validate that a newly discovered YAML adapter or native adapter actually satisfies the protocol before registering it.

## Hot Reload

```python
def reload(self) -> None:
    self._definitions.clear()
    self._scan()
```

`reload()` re-scans the connector directory without restarting the process. Active instances are preserved — only the definitions map is rebuilt. This allows operators to drop a new YAML file into the connectors directory and have it available to new `connect()` calls without downtime.

## Connection Lifecycle

```python
async def connect(self, pocket_id, connector_name, config) -> Any:
    adapter = self._get_or_create(pocket_id, connector_name)
    return await adapter.connect(pocket_id, config)
```

The registry lazily creates adapter instances. If the same pocket connects to the same connector twice, the second call reuses the existing instance (the adapter's `connect()` is idempotent for already-connected adapters). `disconnect()` removes the instance from the map after disconnecting.

## Known Gaps

- **No persistence**: The instance map is in-memory. If the registry is recreated (e.g., server restart), all active connections are lost and must be reconnected.
- **No health monitoring**: There is no background task to detect stale connections or reconnect dropped adapters.
