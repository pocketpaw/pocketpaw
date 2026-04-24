---
{
  "title": "Automations Models — Rule, RuleType, ExecutionMode, and Request Models",
  "summary": "This module defines the Pydantic data models for the EE automations engine: the `Rule` entity (the core automation record), `RuleType` and `ExecutionMode` enums, and `CreateRuleRequest` / `UpdateRuleRequest` for the API layer. The models cover three rule types (threshold, schedule, data_change) with three execution modes (require_approval, auto_execute, notify_only).",
  "concepts": [
    "Rule",
    "RuleType",
    "ExecutionMode",
    "CreateRuleRequest",
    "UpdateRuleRequest",
    "threshold rules",
    "schedule rules",
    "data_change rules",
    "require_approval",
    "auto_execute",
    "cooldown_minutes",
    "linked_intention_id"
  ],
  "categories": [
    "enterprise-edition",
    "automations",
    "data-models",
    "api-models"
  ],
  "source_docs": [
    "cb518d34d513561c"
  ],
  "backlinks": null,
  "word_count": 561,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`models.py` defines the data contracts for the automations engine. Every component — the router, bridge, evaluator, and store — works with these models. Getting the shape right matters: `Rule` must be serializable (JSON storage), API-friendly (request/response validation), and rich enough to express all automation scenarios.

## RuleType Enum

```python
class RuleType(StrEnum):
    THRESHOLD = "threshold"
    SCHEDULE = "schedule"
    DATA_CHANGE = "data_change"
```

Three rule types covering the main automation patterns:
- **threshold** — fires when a metric crosses a value (stock < 10, revenue > $50K)
- **schedule** — fires on a time schedule (cron expression or human-readable preset)
- **data_change** — fires when a watched field changes (order status changed to "shipped")

## ExecutionMode Enum

```python
class ExecutionMode(StrEnum):
    REQUIRE_APPROVAL = "require_approval"
    AUTO_EXECUTE = "auto_execute"
    NOTIFY_ONLY = "notify_only"
```

Execution mode controls the trust level for rule-triggered actions:
- **require_approval** — the agent proposes an action through the Instinct pipeline; the user approves before execution
- **auto_execute** — the action runs immediately without human review
- **notify_only** — no agent action; just delivers a notification

`require_approval` is the default. Auto-execute is intentionally non-default because automated agent actions without oversight is a high-trust operation — users must explicitly opt into it.

## Rule Model

`Rule` is the core entity. Key fields:

```python
class Rule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    pocket_id: str = ""
    name: str
    type: RuleType
    enabled: bool = True
    mode: ExecutionMode = ExecutionMode.REQUIRE_APPROVAL
    cooldown_minutes: int = 60
    linked_intention_id: str | None = None
    last_fired_at: datetime | None = None
    last_evaluated: datetime | None = None
```

**ID generation** uses the first 12 characters of a UUID4, producing short IDs like `a3f8c2d1b9e4`. This is intentionally shorter than full UUIDs for display purposes — automation rule IDs appear in UI labels and notifications.

**`linked_intention_id`** — set by the bridge when a rule is synced to a daemon intention. Used by `unsync_rule_from_daemon` to find and delete the correct intention.

**`cooldown_minutes`** defaults to 60 — prevents rule spam when conditions remain true continuously. Set to 0 to disable cooldown for rules that should fire every cycle.

**`last_evaluated`** tracks when the evaluator last checked this rule, enabling the evaluator to prioritize stale rules or display evaluation history.

## Condition Fields

The condition fields are typed as `str | None` because different rule types use different subsets:

- Threshold rules use: `object_type`, `property`, `operator`, `value`
- Schedule rules use: `schedule`
- Data change rules use: `object_type`, `property` (with `operator = "changed"`)

A single nullable set of fields avoids a complex type hierarchy. The trade-off is that a schedule rule with a `value` field set is technically valid according to Pydantic, even though `value` is meaningless for schedules. Validation of type-specific required fields is left to the router layer.

## Request Models

`CreateRuleRequest` and `UpdateRuleRequest` are slimmer Pydantic models for the API layer. `UpdateRuleRequest` uses `Optional` fields with `None` defaults — only provided fields are applied, enabling partial updates without requiring the full Rule object in every PATCH request.

## Known Gaps

- There is no cross-field validation enforcing that threshold rules have `operator` and `value`, or that schedule rules have `schedule`. A rule created via `CreateRuleRequest` with `type=threshold` and no `value` would pass validation.
- The 12-character UUID prefix has a collision probability higher than full UUIDs. For small rule counts this is negligible, but at enterprise scale (thousands of rules) birthday-attack collisions become possible.
