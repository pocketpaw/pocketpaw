---
{
  "title": "Automation Evaluator: Rule-to-Daemon Sync, Lifecycle, Cooldown, Fire Modes, and Router Integration Tests",
  "summary": "Tests for the `AutomationEvaluator` — the runtime that translates `AutomationStore` rules into daemon intentions, evaluates which rules should fire, and dispatches actions. Covers the full evaluator lifecycle, cooldown enforcement, three execution modes (require approval, auto-execute, notify-only), daemon sync/unsync, and the router bridge integration.",
  "concepts": [
    "AutomationEvaluator",
    "IntentionSpec",
    "daemon sync",
    "cooldown",
    "execution modes",
    "REQUIRE_APPROVAL",
    "AUTO_EXECUTE",
    "NOTIFY_ONLY",
    "schedule rule",
    "threshold stub"
  ],
  "categories": [
    "automations",
    "evaluator",
    "enterprise",
    "testing",
    "test"
  ],
  "source_docs": [
    "2f67a9afe1c3c6ce"
  ],
  "backlinks": null,
  "word_count": 667,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture Context

The evaluator sits between the static `AutomationStore` (rules as configuration) and the live execution layer (the daemon that schedules and runs actions). Its job is to:

1. Convert rules to `IntentionSpec` objects the daemon understands.
2. Sync those intentions to the daemon when rules are created/updated/deleted.
3. On each evaluation cycle, decide which rules have fired and dispatch accordingly.

## TestRuleToIntentionSpec: Rule Translation

Three rule types translate to different intention specifications:

- **Schedule rules** — `cron_expression` maps to a cron-type intention. Predefined schedule names (`hourly`, `daily`, `weekly`) are resolved to cron strings. Unknown schedule names are passed through as-is, allowing raw cron expressions.
- **Threshold rules** — translate to event-driven intentions that the daemon triggers when Fabric emits a matching property change event.
- **Data change rules** — similar to threshold but use the `changed` operator.

`test_rule_to_intention_includes_name_prefix` verifies that the rule `name` is prefixed in the intention spec so daemon-side intentions are identifiable as automation-generated (preventing naming collisions with manually created intentions).

`test_disabled_rule_intention` verifies that disabled rules still produce a valid intention spec — the intention is created but with `enabled=False`. This allows the daemon to track the intention without executing it.

## TestSyncRuleToDaemon

Sync and unsync are fire-and-forget operations that update the daemon's intention registry:

- **`sync_rule_returns_intention_id`** — a successful sync returns the intention ID assigned by the daemon.
- **`sync_rule_returns_none_on_exception`** — if the daemon is unavailable, sync returns `None` rather than raising. Rules must not fail to create just because the daemon is temporarily down.
- **`unsync_rule_calls_delete_intention`** — deletion calls the daemon's `delete_intention` method with the linked intention ID.
- **`unsync_rule_no_linked_intention_is_noop`** — if the rule has no linked intention (e.g., it was never successfully synced), unsync is silent. This prevents double-delete errors during cleanup.

## TestEvaluatorLifecycle

The evaluator has an `is_running` property and `start()`/`stop()` methods:

- `start()` sets `is_running = True`.
- `stop()` sets `is_running = False`.
- **`test_evaluator_start_idempotent`** — calling `start()` twice does not raise or produce duplicate work. This is critical for deployment scenarios where the evaluator might be started by multiple initialization paths.

## TestEvaluatorEvaluateAll: Evaluation Guards

**Disabled rules are skipped** — no `_evaluate_threshold` call is made for disabled rules. This is the primary gate that prevents disabled rules from firing.

**Schedule rules are skipped** — `evaluate_all()` does not evaluate schedule rules; those are handled by the daemon's cron scheduler. Evaluating them inline would cause double-firing.

**Cooldown enforcement** — after a rule fires, it must not fire again until the cooldown period has elapsed. `test_evaluator_respects_cooldown` verifies this. Without cooldown, a threshold rule would fire on every evaluation cycle until the condition clears, flooding the user with duplicate action proposals.

**Fire after cooldown** — `test_evaluator_fires_after_cooldown` verifies that the rule fires again after the cooldown period, using a time mock to advance the clock.

**Threshold returns false by design** — `test_evaluate_threshold_returns_false_by_design` documents that the threshold evaluator is a stub returning `False`. This is an intentional placeholder — the real implementation reads from the Fabric event stream.

## TestFireRule: Execution Modes

Three execution modes determine what happens when a rule fires:

- **`REQUIRE_APPROVAL`** — calls `_propose_action` which creates an Instinct action awaiting human review.
- **`AUTO_EXECUTE`** — calls the daemon directly without going through the approval workflow.
- **`NOTIFY_ONLY`** — neither proposes nor executes; presumably sends a notification (the test verifies neither proposal nor daemon call is made).

`test_fire_rule_increments_fire_count` verifies that `record_fire()` is called after any fire mode, maintaining the fire history regardless of how the action was dispatched.

## TestRouterBridgeIntegration

Three tests verify that the router calls the bridge (which calls the daemon) when rules are created, deleted, or toggled:

- **Create rule** → `sync_rule_to_daemon` is called.
- **Delete rule** → `unsync_rule_from_daemon` is called.
- **Toggle rule** → `sync_rule_to_daemon` is called (to update the enabled state on the daemon).

## Known Gaps

Threshold and data change evaluation are explicitly stubbed as returning `False`. The `test_evaluate_threshold_returns_false_by_design` test locks this behavior, but it means no automation rules of these types can actually fire in the current implementation — the real evaluator is pending.