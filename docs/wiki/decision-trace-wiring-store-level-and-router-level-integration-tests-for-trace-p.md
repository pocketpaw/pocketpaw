---
{
  "title": "Decision Trace Wiring: Store-Level and Router-Level Integration Tests for Trace Persistence and Hydration",
  "summary": "Integration tests that verify `ReasoningTrace` objects are persisted into audit context when `propose()` is called, that `FabricObjectSnapshot` records are keyed to the correct audit row, and that the `/instinct/audit/{id}` hydration endpoint expands trace IDs into full records at the correct depth level.",
  "concepts": [
    "ReasoningTrace",
    "FabricObjectSnapshot",
    "propose()",
    "audit hydration",
    "hydrate parameter",
    "store wiring",
    "router wiring",
    "backward compatibility",
    "audit row",
    "TestClient"
  ],
  "categories": [
    "instinct",
    "audit",
    "testing",
    "api",
    "test"
  ],
  "source_docs": [
    "00ce633f7b7bee7c"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

The trace *model* tests (in `test_decision_traces.py`) verify shape and serialization. This file verifies *wiring* — that the trace actually makes it from the `propose()` call site through the store into the database, and back out through the API with correct expansion behavior.

## TestProposeWithTrace: Store-Level Wiring

### Trace lands in audit context

`test_reasoning_trace_lands_in_audit_context` calls `store.propose()` with a fully populated `ReasoningTrace` and then reads back the audit entries. It deserializes the stored trace from `entry.context["reasoning_trace"]` and asserts that all fields survive the round trip:

```python
decoded = ReasoningTrace.model_validate(proposed[0].context["reasoning_trace"])
assert decoded.fabric_queries == ["obj_acme"]
assert decoded.soul_memories == ["mem_q4_pricing"]
assert decoded.backend == "claude_agent_sdk"
```

This catches the failure mode where the trace is accepted by the store method signature but silently dropped before the SQL write.

### Fabric snapshots keyed to audit row

`test_fabric_snapshots_are_keyed_to_the_audit_row` passes `FabricObjectSnapshot` records with placeholder `audit_id` values. The store must overwrite those placeholders with the real `audit_id` generated when the action is proposed. The test reads back snapshots via `get_snapshots_for_audit(proposed.id)` and checks:

1. Both snapshot `object_id` values are present.
2. Every snapshot's `audit_id` equals the real audit row's `id`.

This prevents orphaned snapshots (snapshots with wrong or missing audit IDs) that could never be retrieved during hydration.

### Backward compatibility: trace is optional

`test_propose_without_trace_still_works` calls `propose()` without a trace. The audit entry must not contain a `reasoning_trace` key. This guards legacy callers — any code written before traces were introduced must continue to function unchanged. The docstring explicitly marks this as intentional: "Trace is optional — legacy callers keep working."

## TestProposeEndpointWithTrace: Router-Level Wiring

`test_endpoint_accepts_and_persists_trace_and_snapshots` drives the full HTTP path using FastAPI's `TestClient`. It posts a JSON payload including a `reasoning_trace` block and a `fabric_snapshots` array and verifies the 201 response contains the correct title and priority. This confirms the Pydantic deserialization, store call, and response serialization all connect correctly.

## TestHydrationEndpoint

The `/instinct/audit/{id}` endpoint supports a `hydrate` query parameter:

- **`hydrate=0` (default)** — returns the raw trace with ID arrays. Fabric snapshots array is empty — IDs are present in the trace but objects are not expanded. Useful for lightweight audit views.
- **`hydrate=1`** — returns the same trace plus the full `FabricObjectSnapshot` records for all referenced object IDs. Useful for deep-dive audit views in the dashboard.

`test_hydrate_zero_returns_decoded_trace_without_expansion` confirms the trace is decoded (not stored as an opaque blob) but snapshots are `[]`.

`test_hydrate_one_returns_snapshots` would verify the expanded form.

`test_hydrate_unknown_audit_returns_404` prevents the endpoint from returning 200 with an empty body when the audit ID does not exist — a common API design mistake.

`test_audit_entry_without_trace_hydrates_empty` ensures that legacy audit entries (no trace stored) return an empty trace structure rather than a 500, covering the mixed-state database that will exist during the rollout period when some entries have traces and some do not.

## Fixture Design

`app_with_store` patches `ee.instinct.router._store` with a `tmp_path`-backed `InstinctStore`, isolating each test from the global singleton. The `client` fixture wraps this with FastAPI's `TestClient` for synchronous HTTP calls.

## Known Gaps

None explicitly flagged. The comment notes this is "PR-B" of a two-PR move, implying PR-A covered the model layer and this PR covers the integration.