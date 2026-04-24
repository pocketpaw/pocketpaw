---
{
  "title": "Plan Mode API — Human Approval Gate for Agent Actions",
  "summary": "The plan mode router provides two endpoints that let users approve or reject an agent's proposed execution plan before any actions are taken. This human-in-the-loop gate prevents agents from running irreversible operations without explicit confirmation.",
  "concepts": [
    "plan mode",
    "human-in-the-loop",
    "agent approval gate",
    "session key",
    "PlanManager",
    "agent loop",
    "execution control",
    "lazy import",
    "idempotency",
    "plan approval"
  ],
  "categories": [
    "agents",
    "API",
    "safety"
  ],
  "source_docs": [
    "2fc5034f8cad9aaf"
  ],
  "backlinks": null,
  "word_count": 495,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports a "plan mode" where the agent pauses after reasoning about what it wants to do and presents the plan to the user for review. Only after explicit approval does the agent proceed with execution. The `plan_mode.py` router is the API surface for that approval gate.

## Why Plan Mode Exists

Autonomous agents can take consequential actions: sending emails, modifying files, calling external APIs, running shell commands. Users who want more control — especially for unfamiliar or high-stakes tasks — can enable plan mode to review the agent's intended steps before they execute. Without this gate, the only recourse for a user who disagrees with an agent's approach is to interrupt mid-execution, which can leave state in an inconsistent condition.

## Endpoints

### `POST /plan/approve`

Looks up the pending plan for the given `session_key` via `get_plan_manager().approve_plan()`. If no active plan is found, it returns 404 rather than silently succeeding. This matters because the UI might issue a duplicate approval (network retry, double-click) — the second attempt should not be interpreted as a successful approval of a nonexistent plan.

On success it returns `PlanActionResponse` with `action="approved"`, signalling the agent loop to resume execution.

### `POST /plan/reject`

The mirror of approve. Calls `pm.reject_plan(body.session_key)`, which signals the agent loop to abandon the planned steps and await new instructions. Returns 404 if there is no active plan to reject — the same idempotency rationale applies.

## Session Key Design

Both endpoints use a `session_key` rather than a plan ID or user ID. The session key scopes approval to a specific active conversation thread, so concurrent sessions with different plans do not interfere. Approving session A does not accidentally resume session B.

## Lazy Import Pattern

Both handlers use late imports:

```python
from pocketpaw.agents.plan_mode import get_plan_manager
```

This is consistent with the rest of the PocketPaw router layer: heavy agent machinery is not imported at startup, keeping the FastAPI process boot time fast. `get_plan_manager()` returns a singleton on first call.

## Integration with the Agent Loop

The plan manager acts as a synchronisation primitive between the HTTP layer and the agent coroutine. When the agent enters plan mode, it suspends itself and writes the pending plan to the manager. The HTTP endpoints let the user signal approval or rejection through the dashboard or any API client, and the agent loop polls or awaits that signal before continuing.

## Known Gaps

- There is no `GET /plan/{session_key}` endpoint to retrieve the current pending plan. The UI must receive the plan through a different channel (WebSocket or SSE events) and cannot re-fetch it if the connection drops.
- Plan expiry is not enforced at the API layer. If a user walks away without approving or rejecting, the session and its pending plan will persist until the process restarts or the session times out at a lower level.
- There is no audit log of which plans were approved or rejected, making it difficult to review an agent's decision history.
