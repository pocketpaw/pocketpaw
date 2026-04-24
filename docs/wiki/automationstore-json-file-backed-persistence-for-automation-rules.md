---
{
  "title": "AutomationStore: JSON File-Backed Persistence for Automation Rules",
  "summary": "The `AutomationStore` class manages CRUD operations and fire-count tracking for automation rules, persisting state to a JSON file at `~/.pocketpaw/automations/rules.json`. It is accessed as a module-level singleton to ensure all router and evaluator calls operate on the same in-memory rule set.",
  "concepts": [
    "AutomationStore",
    "JSON persistence",
    "singleton pattern",
    "write-through cache",
    "fire recording",
    "rule toggle",
    "defensive load",
    "Pydantic model_validate",
    "datetime tracking"
  ],
  "categories": [
    "automations",
    "enterprise edition",
    "persistence",
    "data storage"
  ],
  "source_docs": [
    "8dba6e2629010826"
  ],
  "backlinks": null,
  "word_count": 524,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`AutomationStore` (`src/pocketpaw/ee/automations/store.py`) is the single source of persistence for PocketPaw's automation rules. It uses a JSON file on disk rather than a database, making it zero-dependency for local and self-hosted deployments. The in-memory dictionary (`self._rules`) acts as a write-through cache — every mutating operation updates the dict and flushes to disk immediately.

## Singleton Design

```python
_instance: AutomationStore | None = None

def get_automation_store() -> AutomationStore:
    global _instance
    if _instance is None:
        _instance = AutomationStore()
    return _instance
```

The module-level singleton ensures that the router's HTTP thread and the background evaluator loop both operate on the same in-memory state. Without this, the evaluator would load its own file-backed copy that diverges from what the router mutated, leading to rules firing with stale configuration.

## Write-Through Persistence

Every mutation method calls `self._save()` immediately after updating `self._rules`. The save writes the entire rule list atomically via `Path.write_text`, which on most POSIX systems is a single `write()` syscall that either succeeds or fails completely — preventing partial JSON from landing on disk.

```python
def _save(self) -> None:
    self._path.parent.mkdir(parents=True, exist_ok=True)
    data = [r.model_dump(mode="json") for r in self._rules.values()]
    self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
```

The `default=str` serializer fallback on `json.dumps` ensures that datetime fields (and any other non-JSON-native types introduced by Pydantic models) don't cause a silent save failure.

## Defensive Load

The `_load()` method wraps each rule deserialization in a `try/except`, skipping malformed entries rather than failing startup. This tolerates schema drift: if an older rule file lacks a newly required field, the good rules load while the bad one is logged and dropped.

```python
for item in raw:
    try:
        rule = Rule.model_validate(item)
        self._rules[rule.id] = rule
    except Exception:
        logger.warning("Skipping malformed rule entry: %s", item)
```

The outer `except (json.JSONDecodeError, OSError)` handles the case where the entire file is corrupt or unreadable, logging a warning and starting with an empty rule set rather than crashing the process.

## Fire Recording

```python
def record_fire(self, rule_id: str) -> None:
    rule = self._rules.get(rule_id)
    if rule is None:
        return
    rule.fire_count += 1
    rule.last_fired = datetime.utcnow()
    self._rules[rule_id] = rule
    self._save()
```

The evaluator calls `record_fire` after each successful rule execution. The silent `return` when the rule is not found is intentional: the evaluator may fire a rule that the user concurrently deleted via the API. Raising here would crash the evaluator loop.

## Toggle Semantics

`toggle_rule` flips `rule.enabled` in-memory and saves, then the router re-syncs the new state to the daemon. The toggle sets `updated_at = datetime.utcnow()` to track when the state changed, which is useful for audit and UI freshness checks.

## Known Gaps

- The store has no file-level locking. If multiple processes or threads call `_save()` concurrently, writes could interleave and corrupt the JSON. For single-process use this is safe, but any multi-worker uvicorn deployment would need a lock or database-backed store.
- `list_rules` sorts by `created_at` descending on every call, making it O(n log n) even when only one rule is requested. Caching the sorted list or using an ordered dict would improve performance at scale.
- There is no backup/rotation of the rules file before writing, so a crash mid-write (on network filesystems) could zero out the file.