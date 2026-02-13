# Plan Mode

Require human approval before the agent executes tools. When enabled, tool calls are intercepted, a preview is generated, and the user must approve or reject before execution proceeds.

## Overview

Plan Mode adds a confirmation step between the agent deciding to use a tool and actually executing it. This is useful for:
- Reviewing shell commands before they run
- Approving file writes/edits before they happen
- Auditing agent behavior in real-time

## Approval Flow

```
Agent decides to call a tool
    ↓
PlanManager intercepts the call
    ↓
Human-readable preview generated
    ↓
Preview sent to frontend via WebSocket
    ↓
User reviews in approval modal
    ↓
Approve → tool executes
Reject  → tool skipped, agent notified
Timeout → auto-rejected after 5 minutes
```

## Preview Format

Previews are generated based on tool type:

| Tool | Preview |
|------|---------|
| `shell` | `$ <command>` |
| `write_file` | `Write to /path/to/file (N chars)` |
| `edit_file` | `Edit /path/to/file: "old" → "new"` |
| `read_file` | `Read /path/to/file` |
| Other | Tool name + JSON arguments |

## Controlled Tools

By default, Plan Mode controls these tools:

- `shell` — Shell command execution
- `write_file` — File creation/overwrite
- `edit_file` — File modification

You can customize which tools require approval via `plan_mode_tools`.

## Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `plan_mode` | bool | `false` | Enable Plan Mode |
| `plan_mode_tools` | list | `["shell", "write_file", "edit_file"]` | Tools that require approval |

## Frontend

The approval modal is rendered by:
- `frontend/templates/components/modals/plan_approval.html` — Modal template
- `frontend/js/features/plan_mode.js` — WebSocket handling and UI logic

When a plan needs approval, the modal slides in showing:
- Tool name and preview
- Approve / Reject buttons
- Countdown timer (5 minute timeout)

## Implementation

| File | Description |
|------|-------------|
| `agents/plan_mode.py` | `PlanManager`, `PlanStep`, `ExecutionPlan`, `PlanStatus` |

Key classes:
- **PlanManager** — Manages active plans per session. Methods: `add_step()`, `approve_plan()`, `reject_plan()`, `wait_for_approval()`
- **PlanStep** — Single tool call with preview text and approval status
- **ExecutionPlan** — Collection of steps for a session, serializable to dict for WebSocket transport

## Tests

- `tests/test_plan_mode.py`
