---
{
  "title": "Connector-to-Fabric Pipeline: Stripe Integration, Threshold Rules, and Instinct Proposal Chain Tests",
  "summary": "End-to-end tests for the data ingestion pipeline that runs from a YAML-defined connector (Stripe) through HTTP execution, Fabric object creation, threshold evaluation, and Instinct action proposal — all without real network calls via patched `httpx`. Covers the full chain including error paths and source deduplication behavior.",
  "concepts": [
    "DirectRESTAdapter",
    "YAML connector",
    "Stripe connector",
    "FabricStore",
    "InstinctStore",
    "threshold evaluation",
    "source deduplication",
    "data ingestion pipeline",
    "httpx mock",
    "connector chain"
  ],
  "categories": [
    "connectors",
    "fabric",
    "instinct",
    "testing",
    "test"
  ],
  "source_docs": [
    "12a6fa1922aae160"
  ],
  "backlinks": null,
  "word_count": 533,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why This Pipeline Matters

The connector-to-Fabric-to-Instinct chain is the core value loop: external data comes in via a connector, gets structured into Fabric objects, an automation rule evaluates a condition, and the agent proposes an action for human review. If any link in this chain is broken, the agent goes silent. These tests verify the full chain works end-to-end in a hermetic environment.

## test_connector_to_fabric_full_chain

This is the primary scenario. It runs the complete pipeline:

**Step 1: Parse connector YAML** — `parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")` validates that the Stripe connector definition is readable and parseable. This catches YAML syntax errors and schema drift early.

**Step 2: Connect and execute** — `DirectRESTAdapter.connect()` stores credentials, then `execute("list_invoices")` is called with a patched `httpx.AsyncClient`. The mock returns two invoices: one overdue with a large balance ($125,000), one not overdue with a small balance ($500).

**Step 3: Fabric ingestion** — Invoice data is extracted from the `ActionResult` and used to create `FabricObject` instances of type `Invoice`. The `source_id` field is set to the Stripe invoice ID and `source_connector` to `"stripe"`.

**Step 4: Threshold evaluation** — An inline threshold evaluator checks for `amount_due > 10000`. Only the large invoice fires.

**Step 5: Instinct proposal** — An action is proposed for the overdue large invoice with appropriate context.

**Step 6: Verification** — The test asserts that exactly one action is pending in Instinct and that the audit trail contains `action_proposed`.

## test_connector_not_connected_execute_fails

Verifies the pre-connection guard: calling `execute()` without a prior `connect()` returns `ActionResult(success=False, error="Not connected")`. This prevents the agent from silently doing nothing when credentials were never provided — the agent needs a clear error to surface to the user.

## test_open_invoices_do_not_trigger_large_threshold

Verifies the negative case: when all invoices are below the threshold, zero actions are proposed. This prevents false-positive automations — a system that proposes actions for every invoice regardless of threshold would create alert fatigue.

## test_fabric_source_deduplication

This test documents a subtle behavior: the store does *not* deduplicate by `source_id`. Creating two objects with the same `source_id` from the same connector results in two distinct Fabric objects:

> "The same source_id from the same connector is a distinct object each time it is created."

This is the current behavior, not necessarily the intended final behavior. The test locks it so that if deduplication is added in the future, the change is deliberate and visible.

## Mock HTTP Helper

```python
def _make_mock_httpx_client(json_data: list) -> MagicMock:
    """Return a patched httpx.AsyncClient whose GET returns the given JSON."""
```

The helper returns a fully configured async context manager mock that returns the provided JSON from any GET request. This pattern avoids repetitive `AsyncMock` setup across tests and ensures all tests use consistent HTTP mock behavior.

## Inline Threshold Evaluator

The same inline evaluator appears here as in `test_e2e_brew_and_co.py` and `test_e2e_decision_loop.py`. The comment on each copy notes it replaces the "not-yet-implemented `ee/automations` evaluator." This duplication is technical debt: if the threshold logic changes, three inline copies need updating.

## Known Gaps

Source deduplication is not implemented. The `test_fabric_source_deduplication` test explicitly documents that repeated connector syncs create duplicate objects. A production-ready system would upsert based on `source_id + source_connector`, or at minimum provide a deduplication pass as part of the ingestion pipeline.