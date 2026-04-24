---
{
  "title": "Connector Protocol: YAML Parsing, Registry, and DirectREST Adapter Lifecycle Tests",
  "summary": "This module tests the PocketPaw connector infrastructure — YAML definition parsing, the `ConnectorRegistry` for connector discovery and lifecycle management, and the `DirectRESTAdapter` for connecting, executing actions, and disconnecting from external REST APIs like Stripe.",
  "concepts": [
    "ConnectorProtocol",
    "ConnectorRegistry",
    "DirectRESTAdapter",
    "parse_connector_yaml",
    "TrustLevel",
    "ConnectorStatus",
    "YAML",
    "Stripe connector",
    "CSV connector",
    "action execution",
    "adapter lifecycle",
    "connector discovery"
  ],
  "categories": [
    "connectors",
    "testing",
    "integrations",
    "YAML",
    "test"
  ],
  "source_docs": [
    "56cf43806d191960"
  ],
  "backlinks": null,
  "word_count": 489,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_connectors.py` module covers the connector system that allows PocketPaw agents to call external APIs through declarative YAML definitions. Connectors abstract third-party integrations (Stripe, CSV files, generic REST APIs) behind a uniform protocol, enabling agents to execute actions without hardcoded API client code.

## TestYAMLParsing

The YAML parsing tests read real connector definition files from the `connectors/` directory and validate that `parse_connector_yaml` correctly deserializes them into typed `ConnectorDefinition` objects.

### Stripe Connector
`test_parse_stripe_yaml` asserts:
- `name`, `display_name`, `type`, `icon` are set correctly.
- `auth["method"] == "api_key"` — Stripe requires API key authentication.
- Three actions are defined (`len(defn.actions) == 3`).
- The `create_invoice` action has `trust_level == "confirm"` — requiring user confirmation before execution because it creates a financial record.
- The `sync` config specifies a `table` name for data synchronization.

The trust level test is particularly important: PocketPaw's tool execution model has multiple trust levels (`safe`, `confirm`, `dangerous`). Parsing the YAML incorrectly could silently assign a dangerous action a lower trust level, causing it to execute without confirmation.

### CSV and Generic REST Connectors
`test_parse_csv_yaml` confirms the CSV connector uses `auth["method"] == "none"` — no credentials needed. `test_parse_generic_rest_yaml` confirms the generic REST connector is named `rest_generic` with display name `"REST API"`.

## TestDirectRESTAdapter

The `stripe_adapter` fixture creates a `DirectRESTAdapter` from the Stripe YAML definition. Tests exercise the full adapter lifecycle:

### Connection
- `test_connect_success` calls `adapter.connect({"api_key": "sk_test_..."})` and asserts the adapter transitions to a connected state.
- `test_connect_missing_credential` calls `connect({})` without the required API key and asserts a `ConnectorStatus` indicating failure. This prevents silent half-connected states.

### Action Execution
- `test_list_actions` confirms the adapter exposes the correct action names.
- `test_execute_not_connected` calls `execute` before `connect` and asserts an appropriate error — guarding against use-before-connect bugs.
- `test_execute_connected` connects first, then executes an action, asserting a result is returned.
- `test_execute_unknown_action` calls execute with a nonexistent action name and asserts an error.

### Disconnection and Schema
- `test_disconnect` confirms the adapter can be cleanly disconnected.
- `test_schema` confirms the adapter exposes a JSON schema for each action — used by the agent to construct valid requests.

## TestConnectorRegistry

The `ConnectorRegistry` is a service-locator for all installed connectors:
- `test_scan_connectors` confirms the registry discovers connector YAML files from `CONNECTORS_DIR`.
- `test_get_definition` retrieves a specific connector definition by name.
- `test_connect_and_status` wires up a connector through the registry and checks its status.
- `test_disconnect` disconnects via the registry.
- `test_nonexistent_connector` asserts that looking up a non-existent connector raises an appropriate error.

```python
CONNECTORS_DIR = Path(__file__).parent.parent / "connectors"
```

Using a path relative to the test file means the connector YAML files travel with the test suite.

## Known Gaps

No TODO or FIXME markers. The `test_execute_connected` test mocks or uses a live HTTP call — if it makes real Stripe API calls, it would be environment-dependent. The sync configuration (`defn.sync`) is partially tested but sync execution behavior is not covered. Error handling for malformed YAML files is not tested.