---
{
  "title": "Enterprise Automations Module (Trigger Engine Placeholder)",
  "summary": "The `ee/automations/` module is a reserved namespace for a rule-based trigger engine that will let users define time-based and data-condition-based automations — turning PocketPaw agents from reactive responders into proactive actors.",
  "concepts": [
    "automations",
    "trigger engine",
    "cron scheduler",
    "data-driven triggers",
    "time-based triggers",
    "agent proactivity",
    "event bus",
    "rule engine",
    "enterprise"
  ],
  "categories": [
    "automation",
    "enterprise",
    "agent runtime"
  ],
  "source_docs": [
    "4c244a19d6f3b9e6"
  ],
  "backlinks": null,
  "word_count": 334,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Vision

The two canonical examples in the module comment capture the intent precisely:

- **Data-driven trigger**: "When inventory drops below 10, alert me." — a threshold condition on a monitored data source fires an agent action.
- **Time-driven trigger**: "Every Monday, generate the weekly report pocket." — a cron-style schedule invokes an agent to produce a document.

These two patterns cover the majority of business automation use cases. Together they represent the step from "AI assistant" to "AI agent" in the PocketPaw roadmap: an agent that monitors, decides, and acts without requiring the user to prompt it every time.

## Why a Separate Module?

Automations have a fundamentally different execution model from the request-response HTTP layer. A trigger engine needs:

1. **A scheduler** — either a cron daemon, an APScheduler instance, or a message queue consumer — that fires outside the HTTP request lifecycle.
2. **State management** — tracking which triggers have fired, preventing double-fire on restart, and storing the trigger definitions themselves.
3. **An action dispatcher** — connecting the fired trigger to the agent runtime or the instinct pipeline.

This is substantial infrastructure that should not be mixed into the domain routers or the instinct approval path. A dedicated module with its own scheduler lifecycle keeps the concerns clean.

## Expected Integration Points

When implemented, the automations engine will likely:

- Read trigger definitions from a dedicated SQLite or MongoDB collection.
- Emit events onto the `ee/cloud/realtime/` EventBus so that firing an automation surfaces as a notification in the UI.
- Invoke agents through `pocketpaw.agents.pool.get_agent_pool()` to execute the automated task.
- Write an audit event via `ee/audit/` for every trigger that fires (compliance traceability).

## Known Gaps

- **No implementation exists.** The module is a placeholder docstring as of 2026-03-28.
- The trigger definition schema (how users express "when X, do Y") has not been designed.
- The scheduler technology choice (APScheduler, Celery Beat, a custom loop) is unresolved.
- No UI surface for creating or managing automations has been planned.