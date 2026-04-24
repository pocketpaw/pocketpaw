---
{
  "title": "Brew \u0026 Co. Business Scenario: End-to-End Fabric and Instinct Pipeline Walkthrough",
  "summary": "A narrative-driven end-to-end test that simulates a real small business — a coffee shop on Monday morning — using PocketPaw's `FabricStore` for business objects and `InstinctStore` for the AI decision pipeline. It demonstrates the full operational flow: data modeling, connector sync simulation, threshold-triggered automation, human approval, execution, and audit export.",
  "concepts": [
    "FabricStore",
    "InstinctStore",
    "threshold evaluation",
    "action proposal",
    "approval workflow",
    "audit trail",
    "business scenario",
    "graph traversal",
    "ActionContext",
    "state machine"
  ],
  "categories": [
    "instinct",
    "fabric",
    "testing",
    "e2e",
    "test"
  ],
  "source_docs": [
    "2a9ff702a1cf4504"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Narrative Tests Exist

Unit tests verify individual components; this test verifies that the components *make sense together* in a real business context. The "Brew & Co." framing is intentional: it makes regressions immediately understandable ("the coffee shop scenario broke") and documents the product's value proposition in executable form.

## Test Architecture

No real HTTP calls. All I/O uses `tmp_path` SQLite databases via `FabricStore` and `InstinctStore`. The automation evaluator module is not yet implemented, so threshold evaluation is handled by an inline `_check_threshold()` helper that directly queries the `FabricStore` and applies comparison operators.

## test_brew_and_co_monday: The Core Scenario

The test walks through 12 explicit steps:

**1–2. Schema definition and data loading** — Three object types (`Product`, `Order`, `Customer`) are defined with property schemas. Products are created with inventory levels reflecting a real Monday morning state.

**3–4. Customer and order simulation** — A loyal customer (`visits=47`) is created and linked to her orders via the `placed` relationship. Orders are linked to products via `contains`.

**5. Threshold evaluation** — The inline evaluator checks for `stock < 10`. Only `Oat Milk Latte` (stock=4) fires. Cold Brew (50) and Croissant (12) do not.

**6. Action proposal** — The agent proposes a `HIGH` priority reorder action with a fully populated `ActionContext` including object IDs and numeric metrics:

```python
action = await instinct.propose(
    pocket_id="brew-hq",
    title="Reorder Oat Milk Latte",
    recommendation="Order 20 units from SupplierCo ($44.00). ETA: 2 business days.",
    priority=ActionPriority.HIGH,
    context=ActionContext(
        object_ids=[low_item.id],
        metrics={"current_stock": 4.0, "threshold": 10.0, "reorder_qty": 20.0},
    ),
)
```

**7–9. Approval and execution** — `pending()` shows one action. `approve()` changes status to `approved` and removes it from the pending queue. `mark_executed()` with an order confirmation string changes status to `executed` and records the outcome.

**10. Audit trail** — The audit log must contain all three events in order: `action_proposed`, `action_approved`, `action_executed`.

**11–12. Fabric state and JSON export** — Fabric stats verify object and link counts. `export_audit()` returns a JSON string that parses correctly and contains all three events with required fields (`id`, `actor`, `event`, `timestamp`).

## test_brew_no_actions_when_all_stock_sufficient

Verifies the quiet-day path: when all products are well-stocked (20, 35, 100 units), the threshold evaluator fires for zero products and the Instinct pending queue remains empty. This prevents false-positive action proposals.

## test_brew_multi_customer_order_graph

Verifies that the graph link traversal scales correctly to multiple customers with overlapping order graphs. Alice's two orders and Bob's one order are independent link sets; `get_linked_objects(alice.id, "placed")` must return exactly 2.

## test_brew_rejected_action_does_not_execute

Documents a known behavioral gap: after `reject()` is called, the current store implementation does not enforce state machine transitions — `mark_executed()` succeeds even on a rejected action. The test's comment is explicit:

> "If execution goes through, the test documents current permissive behaviour"

The critical assertion is that the audit trail contains both `action_rejected` and the subsequent execution event, preserving the full decision history even when the state machine is permissive.

## Known Gaps

The inline `_check_threshold()` helper is marked as replacing the "not-yet-implemented `ee/automations` evaluator." Once the real evaluator ships, these tests should be updated to use it. The current approach creates a divergence risk where the inline evaluator and the real evaluator could implement threshold logic differently.