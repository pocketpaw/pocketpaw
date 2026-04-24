---
{
  "title": "Connector Tools: Agent Interface for the Dynamic Connector Registry",
  "summary": "The connector tools module wires PocketPaw's `ConnectorRegistry` into the agent's tool interface, giving the LLM the ability to discover available data connectors, establish connections, execute connector actions, and inspect what actions a connector supports — all through natural language tool calls.",
  "concepts": [
    "ConnectorRegistry",
    "ConnectorListTool",
    "ConnectorConnectTool",
    "ConnectorExecuteTool",
    "ConnectorActionsTool",
    "data connectors",
    "Stripe",
    "REST API",
    "pocket_id",
    "singleton",
    "action introspection"
  ],
  "categories": [
    "tool-system",
    "connectors",
    "data-integration"
  ],
  "source_docs": [
    "47ad02add24bf669"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's connector system allows arbitrary data sources — Stripe, REST APIs, CSV files, databases — to be plugged in as connectors. The connector tools make this system accessible to the agent at runtime without the agent needing to know the connector's internal API. The agent can discover, connect, and query any installed connector through four purpose-built tools.

## Singleton Registry

All four tools share a module-level `_registry: ConnectorRegistry | None` singleton, initialized lazily via `_get_registry()`. The registry is pointed at a `"connectors"` directory (relative to the working directory) where connector manifests and credentials live. The singleton pattern ensures that connector state (connection objects, credentials) is not duplicated across tool instances within a session.

## ConnectorListTool

Returns a structured list of all connectors known to the registry, including their connection status (connected / disconnected / error). This is the discovery entry point — the agent should call this first when a user asks about available integrations. The optional `pocket_id` parameter scopes the list to connectors relevant to a specific pocket (user workspace).

## ConnectorConnectTool

Establishes a connection to a named connector by passing a configuration dictionary. The configuration schema varies by connector type — a REST connector might need a `base_url` and `api_key`, while a CSV connector just needs a `file_path`. The tool delegates validation to the connector's own `connect()` method. On success it returns a confirmation; on failure it surfaces the connector's error message.

```python
async def execute(self, connector_name: str, config: dict, pocket_id: str | None) -> str:
    registry = _get_registry()
    try:
        result = await registry.connect(connector_name, config, pocket_id=pocket_id)
        return json.dumps(result)
    except Exception as e:
        return self._error(str(e))
```

## ConnectorExecuteTool

Executes a named action on an already-connected connector. Actions are connector-specific — a Stripe connector might have actions like `list_customers`, `create_payment_intent`; a REST connector might have `get`, `post`. Parameters are passed as an optional dictionary. This tool is the primary workhorse for data retrieval and mutation through connectors.

## ConnectorActionsTool

Lists all actions available on a specific connector, including their parameter schemas. This is the introspection tool — the agent should call it when it knows which connector to use but is uncertain what actions it supports. The returned schema allows the agent to construct valid `ConnectorExecuteTool` calls without hallucinating parameter names.

## Trust Levels

- `ConnectorListTool` — standard trust (read-only discovery).
- `ConnectorConnectTool` — elevated trust (writes credentials to connector state).
- `ConnectorExecuteTool` — elevated trust (can trigger data mutations).
- `ConnectorActionsTool` — standard trust (read-only introspection).

## Known Gaps

- The `connectors/` directory path is hardcoded as a relative path. In containerized deployments where the working directory is not predictable, this could cause the registry to load from an unexpected location.