---
{
  "title": "Critical Gaps Tests: Real HTTP in YAML Connectors, Fabric Queries, and Instinct Tool Operations",
  "summary": "This test module, created in March 2026, addresses three previously untested integration paths: the `DirectRESTAdapter` making authenticated HTTP calls to YAML-defined connectors like Stripe, the Fabric knowledge-graph query tools, and the Instinct action-proposal tools. It uses real YAML connector definitions from the connectors directory and mocks only the network layer.",
  "concepts": [
    "DirectRESTAdapter",
    "YAML connector",
    "httpx",
    "Stripe connector",
    "local action",
    "auth headers",
    "Bearer token",
    "Fabric",
    "Instinct",
    "action proposal",
    "audit log",
    "parse_connector_yaml",
    "connect guard"
  ],
  "categories": [
    "testing",
    "connectors",
    "HTTP",
    "agent tools",
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

## Overview

PocketPaw's connector system allows agents to interact with external services (Stripe, CSV importers, etc.) through YAML-defined REST adapters. Prior to this test file, the `DirectRESTAdapter.execute()` path had no test coverage for the actual HTTP call construction — only the parsing layer was tested. This file closes those gaps.

## Real HTTP in DirectRESTAdapter (`TestRealHTTP`)

The fixture parses the real `connectors/stripe.yaml` file (not a mock definition) using `parse_connector_yaml`, then constructs a `DirectRESTAdapter` from it. This is important: the test validates against the actual connector specification, not a simplified stub.

### Authentication Headers

`test_execute_builds_auth_headers` calls `connect()` with a `STRIPE_API_KEY` credential dict and then asserts `_build_auth_headers()` returns `{"Authorization": "Bearer sk_test_123"}`. This verifies that the Bearer token injection logic correctly reads the API key from the credential dict, not from a hardcoded location.

### Local Action Bypass

`test_execute_local_action_skips_http` uses the CSV connector, which defines a `import_file` action marked as local. The test asserts `execute()` returns `result.success=True` and `result.data["action"] == "import_file"` without making any HTTP call. This tests the branching logic that distinguishes local actions (file I/O, transformations) from remote API calls.

### Connection Guard

`test_execute_not_connected` calls `execute()` on an adapter that has not had `connect()` called. The expected result is `success=False` and `error == "Not connected"`. This prevents silent failures where an uninitialized adapter makes unauthenticated HTTP requests.

### Unknown Action Handling

`test_execute_unknown_action` calls a non-existent action name after connecting. The result must be `success=False`, preventing `KeyError` from propagating up the stack when the YAML definition doesn't include an action the agent tries to use.

### Full HTTP Call Verification

`test_execute_makes_http_call` is the most comprehensive test: it patches `httpx.AsyncClient`, sets up a mock response with a 200 status and JSON body, calls `execute("list_invoices", {"limit": 5})`, and asserts:
- `result.success=True`
- `result.data` is the parsed list from the mock response
- `mock_client.get.assert_called_once()` — exactly one HTTP GET was made

This test prevents regressions where `execute()` returns success without actually calling the HTTP client.

### HTTP Error Propagation

`test_execute_handles_http_error` simulates a 4xx/5xx response from `httpx` (via `raise_for_status`) and confirms `result.success=False` with the error propagated into `result.error`.

### Basic Auth

`test_build_auth_basic` validates that connectors configured for HTTP Basic authentication produce the correct Base64-encoded `Authorization` header.

## Fabric Tools (`TestFabricTools`)

Fabric is PocketPaw's internal knowledge-graph system. These tests validate the agent tools that query and mutate it:
- `test_fabric_query_no_store`: query with no objects in the store returns an empty result, not an error.
- `test_fabric_query_with_results`: objects added to the store appear in query results.
- `test_fabric_create_object`: creating an object via the tool persists it retrievably.

## Instinct Tools (`TestInstinctTools`)

Instinct is PocketPaw's action-proposal and audit system. Tests cover:
- `test_propose_action`: submitting a proposed action creates a pending entry.
- `test_pending_empty`: the pending queue is empty before any proposals.
- `test_audit_query`: completed actions appear in audit log queries.

## Known Gaps

The file is explicitly named `test_critical_gaps.py` — its existence acknowledges that these were known missing test areas. No TODO markers are present, but the Fabric and Instinct tests are thin (one assertion each), suggesting they were written to establish baseline coverage rather than comprehensive behavioral specifications.