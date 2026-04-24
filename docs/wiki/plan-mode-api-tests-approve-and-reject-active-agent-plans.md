---
{
  "title": "Plan Mode API Tests: Approve and Reject Active Agent Plans",
  "summary": "This test file covers PocketPaw's `/api/v1/plan` router, which lets the dashboard approve or reject an agent's proposed action plan before execution. It verifies the session-keyed lookup, the not-found response when no active plan exists, and input validation.",
  "concepts": [
    "plan mode",
    "PlanManager",
    "session key",
    "approve plan",
    "reject plan",
    "human-in-the-loop",
    "safety mechanism",
    "422 validation",
    "not-found handling",
    "agent action plan"
  ],
  "categories": [
    "plan mode",
    "safety",
    "API",
    "testing",
    "test"
  ],
  "source_docs": [
    "bdd4445df6b89200"
  ],
  "backlinks": null,
  "word_count": 433,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Plan mode is a safety mechanism in PocketPaw: when enabled, the agent formulates a plan and presents it for human review before taking any action. The plan API exposes two endpoints — `POST /plan/approve` and `POST /plan/reject` — that signal the agent to proceed or abort. Both operations are keyed by `session_key`, which ties the HTTP request to the specific conversation session that generated the plan.

## Session Key as Plan Identifier

Plans are not identified by a UUID or database row ID. Instead, `session_key` — the session identifier for the conversation — is used as the lookup key. This design means:

1. Each conversation session can have at most one pending plan at a time.
2. The dashboard knows the session key (it is part of the chat UI state), so no additional plan ID needs to be tracked.
3. Expiry is implicit: if the session ends, the plan is gone.

## Approve (`POST /plan/approve`)

`TestApprovePlan` covers three scenarios:

- **Success**: `pm.approve_plan(session_key)` returns a plan object; the response echoes `session_key` and `action: "approved"`. The test asserts `pm.approve_plan.assert_called_once_with("sess-123")` to confirm the correct key is passed — a bug where the route passes the wrong field would not be caught by a status-code check alone.
- **No active plan**: `pm.approve_plan` returns `None`; the route returns 404 with "no active plan" in the detail. This covers the race condition where the agent's plan times out or the user tries to approve a plan that was already handled.
- **Missing session key**: An empty request body returns 422 (Pydantic validation). The `session_key` field is required; there is no default.

## Reject (`POST /plan/reject`)

`TestRejectPlan` mirrors the approve tests:

- **Success**: `pm.reject_plan(session_key)` returns a plan; response has `action: "rejected"`.
- **No active plan**: Returns 404.
- **Empty session key**: An empty string `""` returns 422. The test specifically uses an empty string rather than a missing field, confirming the validator rejects blank strings in addition to absent values.

## Why Human-in-the-Loop Plan Approval Matters

Without plan mode, an agent receiving a high-consequence instruction (e.g. "delete all logs older than 30 days") would execute immediately. Plan mode forces a pause and surfaces the proposed steps to the user. The approve/reject API is the contract between that pause and the agent's execution engine.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover: what happens when `approve_plan` raises an exception (e.g. the plan manager is not initialised), concurrent approve + reject for the same session key, or what the plan object returned by the manager looks like (its structure is not asserted).