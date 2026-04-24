---
{
  "title": "Enterprise Automations Rule CRUD: Store and FastAPI Router Tests",
  "summary": "Full test coverage for the enterprise `AutomationStore` and its FastAPI router, covering three rule types (threshold, schedule, data change), all CRUD operations, toggle/enable behavior, fire tracking, file persistence, and a complete lifecycle end-to-end test. Every test uses `tmp_path` isolation to avoid touching the user's real `~/.pocketpaw` configuration.",
  "concepts": [
    "AutomationStore",
    "threshold rule",
    "schedule rule",
    "data change rule",
    "CRUD",
    "toggle",
    "fire_count",
    "file persistence",
    "tmp_path isolation",
    "FastAPI router"
  ],
  "categories": [
    "automations",
    "enterprise",
    "testing",
    "api",
    "test"
  ],
  "source_docs": [
    "4027792830817bb7"
  ],
  "backlinks": null,
  "word_count": 558,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Isolation Matters

Automation rules are stored in a file-backed `AutomationStore`. Without `tmp_path` isolation, a test suite crash or assertion error could corrupt the developer's real rule configuration. The fixture comment is explicit:

> `Fresh AutomationStore backed by a temp file — never touches ~/.pocketpaw.`

The `client` fixture additionally patches the module-level singleton store with the `tmp_path`-backed instance, ensuring that even router-level tests that import the singleton get the test store.

## Rule Types

Three `CreateRuleRequest` factory functions produce minimal valid payloads:

- **Threshold** — `property_name`, `operator`, `threshold` fields; triggers when a Fabric property crosses a numeric boundary.
- **Schedule** — `cron_expression` field; triggers on a time schedule.
- **Data Change** — operator is always `"changed"`; triggers when any value in a Fabric object changes.

## TestCreateRuleEndpoint

Three creation tests cover the happy path (threshold, schedule) and the validation path (missing required field returns 422, not 500). The 422 test is critical: it verifies that FastAPI's Pydantic validation is wired up correctly rather than letting malformed requests reach the store.

## TestListRules

Four list tests cover:

- **Returns all** — no filter returns every rule.
- **Filter by pocket** — `pocket_id` filter returns only rules for that pocket. This prevents rules from one pocket appearing in another pocket's automation dashboard.
- **Empty store** — returns an empty list, not a 404 or exception.
- **Sorted newest first** — insertion order is preserved with newest on top. If the UI shows rules in insertion order, a wrong sort direction would flip the display.

## TestToggleRule

Toggle is a common pattern that has a subtle idempotency edge case: toggling twice must return to the original state. `test_toggle_twice` verifies the `enabled` field cycles correctly: `True → False → True`. Without this, a double-click in the UI could silently leave the rule in the wrong state.

`test_toggle_updates_timestamp` verifies that the `updated_at` field changes on toggle. This is used by the frontend to detect stale cache entries.

## TestRecordFire

`record_fire()` is called by the evaluator each time a rule's condition is met. Three tests:

- **Single fire** — `fire_count` increments to 1 and `last_fired` is set.
- **Multiple fires** — `fire_count` increments correctly across multiple calls.
- **Nonexistent rule** — firing a rule that does not exist is a no-op (does not raise). This prevents the evaluator from crashing if a rule is deleted while it is being evaluated.

## TestPersistence

Three persistence tests verify that rules survive across `AutomationStore` instance recreation — simulating a process restart:

- **Single rule survives** — write rule, create new store instance from same file, read rule.
- **Multiple rules survive** — all rules are present after reload.
- **Empty store starts fresh** — a new file starts with zero rules.

Without persistence tests, a bug in the serialization format would only surface after a process restart in production.

## TestFullCrudLifecycle

The end-to-end test exercises Create → Read → Update → Toggle → Delete through the HTTP router in sequence. This catches integration bugs where individual operations work in isolation but fail in combination — for example, a toggle that reads stale cached state after an update.

## Known Gaps

No tests cover concurrent writes to the `AutomationStore`. Since the store is file-backed, simultaneous writes from multiple requests could corrupt the file. The evaluator's cooldown mechanism (`test_evaluator_respects_cooldown` is in a separate file) is not tested here.