---
{
  "title": "Pocket Chat Router — Context-Aware Workspace Creation and Interaction",
  "summary": "The pockets router is PocketPaw's dedicated endpoint for creating and interacting with pocket workspaces — themed dashboards of widgets, charts, and data panels. It streams responses via SSE, injects dynamic Ripple widget documentation for new pockets, and normalises three distinct AI output formats into render-ready Ripple specs.",
  "concepts": [
    "pocket workspace",
    "UISpec",
    "Ripple widgets",
    "SSE streaming",
    "widget normalisation",
    "pocket creation",
    "pocket interaction",
    "kb binary",
    "race condition guard",
    "multi-pane layout",
    "flat widgets",
    "ChatRequest",
    "PocketContext"
  ],
  "categories": [
    "pockets",
    "API",
    "agents",
    "streaming"
  ],
  "source_docs": [
    "2f5536e1e310438c"
  ],
  "backlinks": null,
  "word_count": 580,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A pocket is a persistent workspace: a named dashboard that can contain metrics, charts, tables, feeds, and rich UISpec component trees. The `pockets.py` router is the entry point for both creating new pockets (the agent researches a topic and builds a spec) and interacting with existing ones (the user asks questions or requests modifications inside a live pocket).

## Context Switching: Creation vs. Interaction

The most significant logic gate in the router is:

```python
is_interaction = bool(body.pocket_context and body.pocket_context.id)
```

If the request carries a `pocket_context.id`, the user is already inside a pocket and the agent should help them with it — not create a new one. Sending the creation instructions in this case caused a well-documented bug: the agent would respond "the pocket is empty" and attempt to re-create the pocket from scratch, spawning duplicates.

For creation mode, dynamic Ripple widget documentation is fetched via `_get_ripple_widget_context()` and appended to the system prompt. For interaction mode, this is skipped — widget type reference is only relevant when designing a new spec, not when answering questions about existing content.

## Dynamic Ripple Widget Context

`_get_ripple_widget_context()` calls the `kb` binary (the BM25 knowledge base tool) to search the `ripple` scope for documentation relevant to the user's message. It runs with a 3-second timeout and falls back to an empty string on any failure (binary not found, timeout, non-zero exit). This design makes the knowledge injection a best-effort enhancement — the pocket creation flow succeeds even on machines without the kb binary installed.

Results are wrapped in a `<ripple-widget-reference>` XML tag before being appended to the system prompt, giving the agent a clear structural boundary between static instructions and dynamic reference material.

## Three Output Formats

The `_prepare_pocket_spec()` function normalises three different structures that an AI agent might produce:

1. **Multi-pane UISpec** — `panes` dict with a layout key (`quad`, `workspace`, `split`). Each pane is an independent UISpec tree.
2. **UISpec v1.0** — a single `ui` component tree with nested flex/grid/leaf nodes.
3. **Flat widgets** — a `widgets` array for simple dashboard grids.

Each path validates its required fields and drops malformed entries. Charts require at least 2 numeric data points; metrics require a `value`; tables require both columns and rows. Widgets that fail validation are dropped with a warning log rather than failing the entire pocket.

## Race Condition Guard

```python
bridge = _APISessionBridge(chat_id)
await bridge.start()
# ... then publish the message
```

The bridge subscribes to the message bus **before** the inbound message is published. Without this ordering, the agent could process the message and emit all SSE events before the bridge queue is listening, causing every chunk and `stream_end` to be silently dropped.

## Legacy Bash Extraction

For backwards compatibility, the SSE loop still watches for `tool_start` events containing `create_pocket` commands and extracts the JSON spec using a regex (`_CREATE_POCKET_RE`). This handles agents that invoke pocket creation via the Bash tool rather than the dedicated event channel. The `pocket_emitted` flag ensures only one pocket spec is forwarded per stream.

## Known Gaps

- Pocket metadata (description, `pocket_context.id`) travels through metadata on the inbound message. The full pocket document is not pre-loaded into the system prompt to avoid hitting the Windows CLI arg length limit — instead, the agent fetches it on demand via `get_pocket`. This means the first interaction turn always requires a tool call.
- The `_prepare_widget()` helper and the inline widget transform in `_prepare_pocket_spec()` duplicate the per-type validation logic. A future refactor should unify these paths.
