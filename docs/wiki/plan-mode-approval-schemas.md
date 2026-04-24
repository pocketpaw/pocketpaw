---
{
  "title": "Plan Mode Approval Schemas",
  "summary": "Defines the minimal Pydantic models for PocketPaw's plan-mode API — the human-in-the-loop gate where a user approves or rejects an agent's proposed plan before execution begins. The session key binds the approval action to a specific pending plan.",
  "concepts": [
    "PlanActionRequest",
    "PlanActionResponse",
    "plan mode",
    "human-in-the-loop",
    "session key",
    "agent approval gate",
    "safety checkpoint",
    "Pydantic",
    "agent lifecycle",
    "SSE"
  ],
  "categories": [
    "api-schemas",
    "plan-mode",
    "safety",
    "human-in-the-loop"
  ],
  "source_docs": [
    "7214f484e40a73a2"
  ],
  "backlinks": null,
  "word_count": 505,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Plan mode is a safety mechanism in PocketPaw that pauses agent execution before potentially impactful actions. The agent produces a plan — a structured description of what it intends to do — and waits for the user to explicitly approve or reject it. Only two small Pydantic models are needed: one for the incoming approval action and one for the confirmation response.

## Why Plan Mode Exists

Without a human approval gate, an agent with broad tool access (filesystem writes, API calls, shell commands) could take irreversible actions based on a misunderstanding of the user's intent. Plan mode inserts a checkpoint: the agent explains its intended actions, the user reviews them, and execution only proceeds on explicit approval. This is especially important for destructive operations (file deletion, database mutations, external API calls with side effects).

## Models

### `PlanActionRequest`

```python
class PlanActionRequest(BaseModel):
    session_key: str = Field(..., min_length=1)
```

The approval or rejection of a plan is submitted to a specific endpoint (e.g. `POST /plan/approve` or `POST /plan/reject`), so the action itself is encoded in the URL, not the body. The body only needs to identify *which* pending plan is being acted upon — that's the role of `session_key`.

`session_key` is the runtime identifier for the agent session that is currently paused waiting for approval. The `min_length=1` guard prevents accidentally submitting an empty string, which would either match no session (silent no-op) or, worse, match ambiguously.

Using a session key rather than a session ID is intentional: session keys are short-lived, request-scoped identifiers that change each agent invocation, making replay attacks (submitting an old approval for a new plan) harder.

### `PlanActionResponse`

```python
class PlanActionResponse(BaseModel):
    session_key: str
    action: str  # "approved" or "rejected"
```

Echoes the `session_key` back and confirms the action taken. The echo is important: the client needs to confirm that its approval was applied to the correct session, particularly in multi-session dashboards where several plans might be pending simultaneously.

`action` is documented in a comment as `"approved" or "rejected"`. This is a known weak point — see Known Gaps.

## Integration with the Agent Lifecycle

The typical flow:

1. Agent reaches a plan boundary and emits a `"plan_ready"` SSE event with the plan content.
2. Dashboard displays the plan and presents Approve / Reject buttons.
3. User clicks Approve → dashboard POSTs `PlanActionRequest` to `/api/v1/plan/approve`.
4. Backend unblocks the agent session; agent proceeds to execute.
5. Dashboard receives a `PlanActionResponse` confirming the session was approved.

The SSE event and these REST schemas together form the complete plan-mode protocol.

## Known Gaps

- `action: str` on `PlanActionResponse` has no type constraint — it should be `Literal["approved", "rejected"]` to make exhaustive handling enforceable by type checkers.
- No timeout model: if the user never approves or rejects, the session sits paused indefinitely. A `PlanTimeoutResponse` or TTL field would make timeout behaviour explicit in the schema.
- No way to modify a plan before approval — the user can only approve or reject, not suggest edits. A `PlanAmendRequest` would support more collaborative human-agent workflows.