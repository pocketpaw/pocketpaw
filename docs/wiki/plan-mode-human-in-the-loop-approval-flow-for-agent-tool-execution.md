---
{
  "title": "Plan Mode — Human-in-the-Loop Approval Flow for Agent Tool Execution",
  "summary": "Implements `PlanManager` and `ExecutionPlan`, which intercept agent tool calls and hold them pending user approval before execution. Plans have a per-session lifecycle with timeout-based auto-rejection, providing an auditable gate between agent intent and system-modifying actions.",
  "concepts": [
    "PlanMode",
    "PlanManager",
    "ExecutionPlan",
    "PlanStep",
    "human-in-the-loop",
    "tool approval",
    "PlanStatus",
    "auto-rejection",
    "timeout",
    "session isolation",
    "generate_preview"
  ],
  "categories": [
    "agent-runtime",
    "security",
    "approval-flow",
    "tool-use"
  ],
  "source_docs": [
    "2c43ef97513c5af6"
  ],
  "backlinks": null,
  "word_count": 405,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Plan Mode adds a human-in-the-loop gate between an agent deciding to call a tool and that tool actually executing. When active, proposed tool calls are collected into an `ExecutionPlan`, rendered as a readable preview, and held until the user explicitly approves or rejects them — or until the plan expires.

## Why Plan Mode Exists

Without plan mode, every tool call executes immediately. For high-stakes operations (deleting files, sending emails, running shell commands), users may want to review the agent's intent before allowing execution. Plan mode transforms the agent from an autonomous executor into a proposer that waits for human sign-off.

## Plan Lifecycle

`PlanStatus` tracks five states:

1. **PROPOSED** — plan created, awaiting user decision
2. **APPROVED** — user approved; execution may proceed
3. **REJECTED** — user rejected; execution is aborted
4. **EXECUTING** — steps are running
5. **COMPLETED** — all steps finished

The transition from PROPOSED to APPROVED/REJECTED happens via `wait_for_approval()`, which blocks on an `asyncio.Event` with a configurable timeout.

## Human-Readable Step Previews

`PlanStep.generate_preview()` renders each proposed tool call as a short, readable string:

- Shell/Bash: `$ <command>`
- Write: `Write to <path>:
<first 200 chars of content>`
- Edit: `Edit <path>`
- Read: `Read <path>`
- Unknown: `tool_name(param=value, ...)`

This is what users see in the approval UI. Raw JSON dicts are unreadable and could be gamed to hide the actual operation; the preview makes intent transparent.

## Timeout-Based Auto-Rejection

`wait_for_approval()` wraps an `asyncio.Event.wait()` in `asyncio.wait_for()` with a configurable timeout. If the user does not respond within the window, the plan is auto-rejected. This prevents the agent from hanging indefinitely on asynchronous channels (Telegram, Discord) where users may not be watching in real time.

## Session Isolation with Singleton Manager

`get_plan_manager()` returns a process-level singleton `PlanManager`. Each active session has at most one pending plan, keyed by `session_key`. If the agent proposes a second plan before the first is approved, `create_plan()` replaces the old one — avoiding an accumulation of stale approvals.

`ExecutionPlan.to_preview()` joins all step previews into a single block suitable for displaying to the user in one message, with step numbers prepended.

## Known Gaps

- Plan approval requires the channel adapter to surface the preview and signal back the user's decision; no default approval UI is provided.
- No audit log: approved and rejected plans are not persisted.
- `create_plan()` discarding the old plan means a user who was reviewing a first plan loses it silently.
