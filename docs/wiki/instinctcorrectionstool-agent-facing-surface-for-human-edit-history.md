---
{
  "title": "InstinctCorrectionsTool: Agent-Facing Surface for Human Edit History",
  "summary": "The `InstinctCorrectionsTool` (tool name: `instinct_corrections`) lets an agent fetch the history of human corrections applied to previously proposed actions within a pocket before generating its next proposal. By loading past edits into context, the agent can match the user's preferred tone, thresholds, and style without requiring repeated instruction — effectively turning human corrections into implicit few-shot examples for future drafts.",
  "concepts": [
    "InstinctCorrectionsTool",
    "instinct_corrections",
    "human corrections",
    "feedback loop",
    "lazy import",
    "ee module",
    "pocket_id",
    "correction_soul_bridge",
    "soul-protocol",
    "trust level",
    "BaseTool"
  ],
  "categories": [
    "builtin tools",
    "instinct system",
    "human-in-the-loop",
    "enterprise features"
  ],
  "source_docs": [
    "c399014a31b8a1dc"
  ],
  "backlinks": null,
  "word_count": 563,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`instinct_corrections.py` was created 2026-04-12 as part of "Move 1 PR-B" — the first phase of the Instinct system rollout. It is the agent-facing complement to the `correction_soul_bridge`, which injects the same correction signal into soul-protocol's automatic memory when a soul is loaded.

The tool's purpose is to close the feedback loop: when the agent proposes an action and the user edits it before approving, that edit is a rich learning signal. Without this tool, the correction is stored in the Instinct store but never surfaced to the agent at draft time. With this tool, the agent can load recent corrections before generating its next proposal and align its output to the user's established preferences.

## The learning signal

Corrections are qualitatively different from explicit instructions. When a user edits a proposed email subject from "RE: Invoice" to "Following up on INV-2024-089" three times, they have implicitly communicated a preference for specific subject lines without ever stating it as a rule. `InstinctCorrectionsTool` surfaces these implicit signals as structured records the agent can reason about.

The tool's description is unusually detailed for this reason — it explains not just what the tool returns but how the agent should use it:

```
"Use this BEFORE proposing a new action so your draft already matches the style
and thresholds the user prefers — e.g. if they consistently edit the greeting
tone or cap a discount percentage, match that pattern in the new proposal."
```

This guidance is embedded in the description so it applies in any pocket context, not just in sessions where the system prompt explicitly mentions corrections.

## Parameters

- `pocket_id` (required): Scopes the correction fetch to a specific pocket. Corrections from other pockets are irrelevant — the user's email style in one pocket may differ from their calendar style in another.
- `limit` (optional, default 10): Returns the `limit` most recent corrections. Older corrections are less relevant as preferences evolve.

## Lazy import and graceful degradation

```python
def _get_instinct_store():
    """Lazy import — degrades gracefully when ee/ is not installed."""
    try:
        from ee.api import get_instinct_store
        return get_instinct_store()
    except ImportError:
        return None
```

The Instinct store lives in `ee/` (enterprise edition). On community installations, the import fails and returns `None`. The `execute` method checks for `None` and returns a clear message like `"Instinct store not available"` rather than crashing. This follows the same lazy import pattern as `FabricQueryTool`.

## Pairing with correction_soul_bridge

The tool comment explicitly notes the pairing with `correction_soul_bridge`. The two components serve different consumers of the same signal:

- `correction_soul_bridge` injects corrections into the soul's memory so they persist across sessions and are available to any agent that loads the soul.
- `InstinctCorrectionsTool` provides on-demand access to the raw correction history within a single session, with filtering by pocket and recency.

Together they ensure the correction signal is available both as persistent memory (soul) and as precise, queryable history (tool).

## Trust level

`trust_level = "high"` — corrections contain the user's edited content, which may include sensitive business information like pricing thresholds, customer names, or communication style details.

## Known Gaps

- **Read-only**: The tool only reads corrections; it cannot submit corrections programmatically. Corrections are created through the UI approval flow only.
- **No cross-pocket aggregation**: `pocket_id` is required, preventing the agent from asking "what are my most common corrections across all pockets?" which would be useful for understanding global preferences.