---
{
  "title": "Instinct Tools: Action Proposal, Pending Queue, and Decision Audit",
  "summary": "The `instinct_tools.py` module provides three `BaseTool` subclasses — `InstinctProposeTool`, `InstinctPendingTool`, and `InstinctAuditTool` — that form the agent-facing surface of PocketPaw's human-in-the-loop decision pipeline. The agent proposes actions rather than executing them autonomously; humans approve or reject from a queue; all decisions are recorded in an audit log the agent can query.",
  "concepts": [
    "InstinctProposeTool",
    "InstinctPendingTool",
    "InstinctAuditTool",
    "instinct_propose",
    "instinct_pending",
    "instinct_audit",
    "human-in-the-loop",
    "approval queue",
    "audit log",
    "lazy import",
    "ee module",
    "BaseTool",
    "decision pipeline"
  ],
  "categories": [
    "builtin tools",
    "instinct system",
    "human-in-the-loop",
    "enterprise features"
  ],
  "source_docs": [
    "61760f25aec41f6c"
  ],
  "backlinks": null,
  "word_count": 554,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`instinct_tools.py` was created 2026-03-28 as the primary interface between the agent and PocketPaw's Instinct decision pipeline. The Instinct system is designed around a core principle: for high-stakes actions (reordering inventory, flagging invoices, sending emails), the agent should propose and explain, not act. A human reviews the proposal and approves or rejects it. This keeps the human in control while still leveraging the agent's analytical capabilities.

## InstinctProposeTool

Tool name: `instinct_propose`. Trust level: `medium` — proposing is safer than executing but still affects the user's workflow. Parameters:

- `pocket_id` (required): The pocket context for the proposal.
- `title` (required): Short summary of the proposed action.
- `recommendation` (required): The specific recommended action.
- `description` (optional): Detailed explanation of the reasoning.
- `priority` (optional): Urgency signal (`low`, `medium`, `high`).
- `category` (optional): Action category for grouping in the approval queue.
- `reason` (optional): Why this action is being proposed now.

The description instructs the agent to use this tool when it has "analyzed data and wants to recommend an action." The examples in the description — `"reorder inventory"`, `"flag suspicious invoice"`, `"send reminder email"` — calibrate the agent's judgment about what rises to the level of a proposal.

## InstinctPendingTool

Tool name: `instinct_pending`. Returns all pending actions in the approval queue for a given pocket. This allows the agent to check whether a similar action was already proposed (avoiding duplicate proposals) or to report on the backlog of decisions awaiting human review.

The pending queue is scoped by `pocket_id` because different pockets serve different workflows and different humans — an inventory pocket's pending actions are irrelevant to an email pocket.

## InstinctAuditTool

Tool name: `instinct_audit`. Queries the decision audit log — a persistent record of all approved and rejected proposals with timestamps. Parameters: `pocket_id` and `limit` (default 20).

The audit log serves two purposes:

1. **Agent learning**: The agent can read past approvals to understand what kinds of actions the user tends to approve, calibrating future proposals.
2. **Compliance**: For regulated workflows, the audit log provides a record of every human decision made in the pipeline.

## Lazy import pattern

```python
def _get_instinct_store():
    """Lazy import to avoid circular deps and missing ee/ module."""
    try:
        from ee.api import get_instinct_store
        return get_instinct_store()
    except ImportError:
        return None
```

Identical to the pattern in `instinct_corrections.py` and `fabric_tools.py`. The Instinct store is enterprise-only; on community builds, all three tools return a graceful "not available" message.

## The proposal-not-execution principle

The tool description for `InstinctProposeTool` emphasizes that the action "goes into the approval queue — the user approves or rejects it." This framing is deliberate: it instructs the agent to treat proposal as the terminal action, not execution. Without this instruction, an agent might propose and then immediately attempt to execute the same action, bypassing the human review step.

## Known Gaps

- **No bulk proposal**: `InstinctProposeTool` submits one proposal per call. Batch analysis that identifies 50 reorder candidates would require 50 tool calls.
- **No proposal update**: Once submitted, a proposal cannot be edited by the agent. If the agent realizes the proposal is wrong, it must wait for the user to reject it, then submit a corrected one.
- **No notification mechanism**: The agent cannot trigger a notification to the user that a proposal is awaiting review. The user must poll the pending queue or check the UI.