---
{
  "title": "Full Decision Loop: Fabric Threshold Detection to Instinct Audit Export Tests",
  "summary": "End-to-end tests for the complete decision loop from Fabric object creation through threshold rule evaluation to Instinct action proposal, approval, and JSON audit export — all using real store implementations with `tmp_path` SQLite databases. Three scenarios cover single trigger, multiple triggers, and audit trail identity.",
  "concepts": [
    "decision loop",
    "threshold evaluation",
    "FabricStore",
    "InstinctStore",
    "audit export",
    "action proposal",
    "approval workflow",
    "multiple triggers",
    "audit identity",
    "JSON export"
  ],
  "categories": [
    "instinct",
    "fabric",
    "testing",
    "e2e",
    "test"
  ],
  "source_docs": [
    "014a05e68f7fd84f"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

These tests validate the pipeline that powers PocketPaw's core intelligence loop: business data is stored in Fabric, an automation rule evaluates a condition, the agent proposes an action, a human approves it, and the system records a complete audit trail. If this loop breaks, the entire agentic value proposition fails.

## test_full_decision_loop: Step-by-Step

### Schema and Data

An `Inventory` object type is defined with two properties: `name` (string, required) and `quantity` (number, required). Three objects are created: Oat Milk (qty=4), Coffee Beans (qty=50), Cups (qty=200).

The type ID format is verified: `assert inv_type.id.startswith("ot-")`. This enforces the naming contract between the store and any code that parses IDs by prefix.

### Threshold Evaluation

The inline `_evaluate_threshold()` helper queries all `Inventory` objects and applies comparison operators via a `match` statement:

```python
match operator:
    case "lt":
        match = val < threshold
    # ...
```

Only Oat Milk (4 < 10) fires. The test explicitly asserts that Coffee Beans and Cups do not appear in the result — negative assertions matter as much as positive ones when the rule is "do not spam the user with false alerts."

### Action Proposal

One action is proposed per triggered object. The proposal includes a human-readable recommendation string formatted with the object's current quantity. The `action.id` prefix (`act-`) is verified for the same reason as the type ID prefix.

### Pending and Approval

`instinct.pending()` returns the proposed actions. `instinct.approve(action.id, "user:prakash")` changes status to `approved` and records the approver. The pending queue is empty after approval.

### Execution

`instinct.mark_executed(action.id, outcome_string)` transitions to `executed` status. The outcome string ("Order #ORD-... placed...") is stored and retrievable.

### Audit Export

`instinct.export_audit("store-hq")` returns JSON. The test parses it and verifies:

- All three events are present: `action_proposed`, `action_approved`, `action_executed`.
- Every entry has the required fields: `id`, `actor`, `event`, `timestamp`.

This export is the compliance artifact — the thing a business owner would review to understand what the agent did and why.

## test_multiple_low_stock_items_all_get_actions

When three items are all below threshold, one action is proposed per item. The test verifies that the loop over triggered objects creates the correct number of proposals, and that all three are in the pending queue simultaneously.

This covers the "busy Monday" scenario where multiple items hit their thresholds at the same time. A bug where only the first triggered item gets an action would go unnoticed in the single-trigger test.

## test_approved_action_audit_contains_approver

Verifies that `audit_trail[approver].actor` contains the approver identity (`"user:prakash"`). This is critical for accountability: the audit trail must record *who* approved, not just *that* it was approved.

The test queries audit entries filtered to `action_approved` events and checks the `actor` field, confirming the approver identity survives the store round trip.

## Known Gaps

The inline `_evaluate_threshold()` function is explicitly labeled as a replacement for the "not-yet-implemented `ee/automations` evaluator." The `match` statement implementation is duplicated across three test files. When the real evaluator ships, these inline copies should be removed.