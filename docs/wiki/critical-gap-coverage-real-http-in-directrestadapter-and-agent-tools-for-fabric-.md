---
{
  "title": "Critical Gap Coverage: Real HTTP in DirectRESTAdapter and Agent Tools for Fabric and Instinct",
  "summary": "This test file was written specifically to close gaps in test coverage identified during a gap analysis — areas where code existed but no test exercised it end-to-end. It covers real HTTP dispatch through `DirectRESTAdapter`, the Fabric agent tools (`FabricQueryTool`, `FabricCreateTool`), and the Instinct agent tools (`InstinctProposeTool`, `InstinctPendingTool`, `InstinctAuditTool`).",
  "concepts": [
    "DirectRESTAdapter",
    "httpx",
    "YAML connector engine",
    "Bearer auth",
    "Basic auth",
    "FabricQueryTool",
    "FabricCreateTool",
    "InstinctProposeTool",
    "ActionResult",
    "connector YAML"
  ],
  "categories": [
    "connectors",
    "agent-tools",
    "testing",
    "http",
    "test"
  ],
  "source_docs": [
    "96bb393a309b7193"
  ],
  "backlinks": null,
  "word_count": 510,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why This File Exists

The name "critical gaps" signals intent: this suite was added after a review found that `DirectRESTAdapter.execute()` had never been tested with a real (mocked) HTTP call. The YAML-driven connector engine could parse definitions and build headers, but nobody had verified that the actual `httpx.AsyncClient` invocation used the correct method, URL, and auth headers. A misconfigured auth header would silently succeed in unit tests while failing against any real API.

## TestRealHTTP

All tests use a `stripe_adapter` fixture that parses the real `stripe.yaml` connector definition via `parse_connector_yaml`, giving confidence that the YAML parsing pipeline is also exercised.

**Auth header construction** — `test_execute_builds_auth_headers` verifies the Bearer pattern: `Authorization: Bearer sk_test_123`. This prevents future refactors from silently breaking the auth scheme.

**Local action bypass** — `test_execute_local_action_skips_http` uses the `csv.yaml` connector, which defines a local (non-HTTP) `import_file` action. The adapter must return a synthetic success result without touching the network. This ensures the YAML `local: true` flag is respected and no accidental HTTP calls leak into local-only connectors.

**Pre-connection guard** — `test_execute_not_connected` calls `execute()` before `connect()`. The adapter must return `ActionResult(success=False, error="Not connected")` rather than crashing. Without this guard, an agent that calls a connector out of order would get an unhandled exception instead of a recoverable error.

**HTTP dispatch** — `test_execute_makes_http_call` patches `httpx.AsyncClient` and verifies that `execute("list_invoices", {"limit": 5})` calls `client.get()` exactly once and maps the JSON response to `result.data`. This is the core correctness test for the HTTP dispatch layer.

```python
with patch("httpx.AsyncClient", return_value=mock_client):
    result = await stripe_adapter.execute("list_invoices", {"limit": 5})
assert result.success is True
assert result.data[0]["id"] == "inv_1"
mock_client.get.assert_called_once()
```

**HTTP error handling** — `test_execute_handles_http_error` verifies that a 401 `HTTPStatusError` is caught and converted to `ActionResult(success=False, error="...")` rather than propagating. Uncaught HTTP errors would crash the agent mid-turn.

**Basic auth** — `test_build_auth_basic` tests that the `basic` auth method produces a `Basic <base64>` header, covering a second auth scheme beyond Bearer.

## TestFabricTools

These tests verify the agent tool wrappers around `FabricStore`. The tools exist so agents can query and create business objects without needing direct store access.

- **`test_fabric_query_no_store`** — when the enterprise store is unavailable, the tool returns an "not available" message rather than raising, preventing agent crashes in community tier.
- **`test_fabric_query_with_results`** — formats a `FabricQueryResult` into readable text including object count and property values.
- **`test_fabric_create_object`** — calls `create_object` and returns a confirmation string the agent can relay to the user.

## TestInstinctTools

Three tools cover the Instinct decision pipeline:

- **`InstinctProposeTool`** — lets an agent propose a new action; returns a formatted string with the action title and "pending" status so the agent knows to wait for approval.
- **`InstinctPendingTool`** — returns "all clear" when no actions are pending, allowing the agent to report clean state without null-checking.
- **`InstinctAuditTool`** — queries the audit log and formats entries so the agent can explain recent decisions to the user.

## Known Gaps

The `unknown_action` test confirms that executing an unrecognized action returns `success=False`, but does not verify the error message text — a future test could lock that contract to prevent silent message changes.