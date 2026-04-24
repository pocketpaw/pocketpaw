---
{
  "title": "Automation Evaluator — Background Condition Polling Engine",
  "summary": "AutomationEvaluator is a singleton background loop that periodically checks threshold and data_change automation rules against live Fabric data, then fires matched rules through the Instinct pipeline (for approval-required rules) or directly via the daemon (for auto-execute rules). Schedule rules are handled by the bridge/daemon and do not go through the evaluator.",
  "concepts": [
    "AutomationEvaluator",
    "background loop",
    "condition polling",
    "threshold evaluation",
    "data_change detection",
    "cooldown guard",
    "ExecutionMode",
    "REQUIRE_APPROVAL",
    "AUTO_EXECUTE",
    "NOTIFY_ONLY",
    "Fabric data",
    "singleton pattern"
  ],
  "categories": [
    "enterprise-edition",
    "automations",
    "background-tasks",
    "condition-evaluation"
  ],
  "source_docs": [
    "6fb6c59b3bef7436"
  ],
  "backlinks": null,
  "word_count": 499,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The evaluator answers a simple question on a timer: *which automation rules have their conditions met right now?* Schedule rules are handled by the daemon (they fire on cron triggers). The evaluator focuses on the condition-based rules — threshold and data_change — that require live data comparison.

## Singleton Pattern

`get_evaluator(interval)` returns a module-level singleton:

```python
_evaluator: AutomationEvaluator | None = None

def get_evaluator(interval: int = 60) -> AutomationEvaluator:
    global _evaluator
    if _evaluator is None:
        _evaluator = AutomationEvaluator(interval_seconds=interval)
    return _evaluator
```

The singleton ensures only one background loop runs per process. The router's start/stop endpoints call `get_evaluator().start()` and `get_evaluator().stop()` — without a singleton, starting via the router and stopping via a different code path could reference different instances.

## Evaluation Loop

`_loop()` runs until `stop()` is called, sleeping `interval_seconds` between evaluation cycles. Each cycle calls `_evaluate_all()`, which:

1. Loads all enabled rules from the store
2. For each threshold rule, calls `_evaluate_threshold(rule)`
3. For each data_change rule, calls `_evaluate_data_change(rule)`
4. Fires matched rules via `_fire_rule(rule)`

## Threshold Evaluation

`_evaluate_threshold(rule)` fetches the current value of `rule.property` for `rule.object_type` from Fabric data, then applies the comparison operator (`less_than`, `greater_than`, `equals`). Returns `True` if the condition is met.

The Fabric data source is the EE workspace resource mesh — it provides structured access to business object data (products, orders, metrics) without requiring custom integrations per object type.

## Data Change Detection

`_evaluate_data_change(rule)` compares the current value against the stored `last_value` in the rule. If the value differs, the condition fires and the rule's `last_value` is updated. This is poll-based change detection — the evaluator doesn't receive real-time change events, it snapshots and compares.

The poll-based approach means changes between evaluation cycles are detected with up to `interval_seconds` latency. This is acceptable for business-level automations (stock alerts, revenue tracking) where sub-minute latency is not required.

## Firing Rules

`_fire_rule(rule)` routes based on `ExecutionMode`:

- **`REQUIRE_APPROVAL`** — calls `_propose_action(rule)`, which routes through the Instinct pipeline. The agent proposes an action and waits for human approval before executing.
- **`AUTO_EXECUTE`** — calls `_execute_directly(rule)`, which calls the daemon to execute the configured action immediately without approval.
- **`NOTIFY_ONLY`** — calls `_notify(rule)`, which sends a channel notification without any agent action.

## Cooldown Guard

Before firing, the evaluator checks `rule.cooldown_minutes` against `rule.last_fired_at`. If the rule fired within the cooldown window, it skips — preventing spam when a condition remains true across multiple evaluation cycles. This is a stateful idempotency guard: without it, a stock-below-threshold condition would generate a notification every 60 seconds indefinitely.

## Known Gaps

- `_evaluate_threshold` and `_evaluate_data_change` do not handle Fabric data fetch failures — an exception from the data layer would propagate up and potentially crash the evaluation loop.
- There is no per-rule error isolation: if one rule's evaluation raises, subsequent rules in the same cycle may not be evaluated.
- The cooldown check relies on `rule.last_fired_at` being persisted correctly by the store. If the store update fails after firing, the rule could fire again immediately on the next cycle.
