---
{
  "title": "Automations Router: REST API for Rule-Based Pocket Automations",
  "summary": "The automations router exposes a FastAPI-based CRUD API for managing automation rules and controlling the background evaluator that fires them. It acts as the HTTP boundary between the frontend and the lower-level store, bridge, and evaluator subsystems, ensuring that every rule mutation is immediately synchronized to the daemon via the bridge.",
  "concepts": [
    "AutomationRule",
    "AutomationStore",
    "daemon bridge",
    "evaluator",
    "intention sync",
    "CRUD router",
    "FastAPI",
    "toggle endpoint",
    "idempotent start",
    "rule persistence"
  ],
  "categories": [
    "automations",
    "enterprise edition",
    "REST API",
    "background processing"
  ],
  "source_docs": [
    "9aab1145d65ff415"
  ],
  "backlinks": null,
  "word_count": 576,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The automations router (`src/pocketpaw/ee/automations/router.py`) is the HTTP entry point for PocketPaw's rule-based automation system. Mounted at `/api/v1/automations`, it coordinates three internal components: the `AutomationStore` (JSON-backed persistence), the `bridge` (daemon synchronization), and the `evaluator` (background rule engine).

## Rule CRUD Endpoints

The router implements the standard REST pattern for automation rules:

```python
router = APIRouter(prefix="/automations", tags=["Automations"])

@router.post("/rules", response_model=Rule, status_code=201)
async def create_rule(body: CreateRuleRequest):
    store = get_automation_store()
    rule = store.create_rule(body)
    intention_id = sync_rule_to_daemon(rule)
    if intention_id:
        store.update_rule(rule.id, UpdateRuleRequest(linked_intention_id=intention_id))
        rule.linked_intention_id = intention_id
    return rule
```

The critical design choice here is the **post-create daemon sync**. After persisting a rule to the store, the router immediately calls `sync_rule_to_daemon()`, which creates a linked intention in the core daemon. The `intention_id` is then written back to the store record. This two-phase write exists because rule evaluation doesn't happen inside the API process — the daemon evaluator is a separate background component, and without the `linked_intention_id`, the daemon would have no knowledge of the rule.

On update, the same sync is applied with a guard: if `sync_rule_to_daemon` returns a new intention ID that differs from the one stored on the rule, the store is updated again. This prevents orphaned daemon intentions from accumulating when rules are modified.

On delete, the **unsync happens before the store delete**. Calling `unsync_rule_from_daemon(rule)` first ensures the daemon intention is removed even if the store delete subsequently fails. The alternative — deleting from the store first — would leave a ghost intention that would keep firing with no rule to update.

## Toggle Endpoint

```python
@router.post("/rules/{rule_id}/toggle", response_model=Rule)
async def toggle_rule(rule_id: str):
    rule = store.toggle_rule(rule_id)
    sync_rule_to_daemon(rule)  # pushes enabled state change
    return rule
```

The toggle endpoint uses a `POST` rather than `PATCH` because it is a state-flipping action with no body — the new state is derived by the store, not provided by the client. After toggling, the rule is re-synced so the daemon knows immediately whether to execute this rule's intention.

## Evaluator Control Endpoints

The router exposes three evaluator management endpoints (`/evaluator/start`, `/evaluator/stop`, `/evaluator/status`). These allow operators to control the background polling loop without restarting the process. Each endpoint is idempotent by design: starting an already-running evaluator returns `already_running` rather than spawning a second instance, and stopping an already-stopped evaluator returns `already_stopped`.

```python
@router.post("/evaluator/start")
async def start_evaluator():
    evaluator = get_evaluator()
    if evaluator.is_running:
        return {"ok": True, "status": "already_running"}
    evaluator.start()
    return {"ok": True, "status": "started"}
```

This guard prevents double-start bugs where rapid API calls or misconfigured orchestration would attempt to launch multiple concurrent evaluator loops.

## Error Handling Pattern

The router uses a consistent 404 pattern: if the store returns `None` or raises `KeyError`, the router surfaces an `HTTPException(status_code=404)` with the rule ID embedded in the message. This prevents generic "not found" errors from leaking opaque internal state while still giving clients a precise, actionable message.

## Known Gaps

- There is no authentication guard on these endpoints in the visible source. In the EE build, the router is presumably mounted behind auth middleware, but the router itself does not declare any `Depends()` for access control — making it important not to expose this router in OSS builds.
- The toggle endpoint always re-syncs to the daemon, even when sync is a no-op (e.g., rule has no `linked_intention_id`). The bridge's `sync_rule_to_daemon` would need to handle this gracefully, but it is not visible here.
- No rate limiting or concurrency guard exists on `/evaluator/start` — a fast client could call it multiple times before the `is_running` flag propagates.