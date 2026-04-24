---
{
  "title": "Pocket Tools: Create, Add Widget, and Remove Widget for Ripple UI Specs",
  "summary": "The `pocket.py` module provides three `BaseTool` subclasses — `CreatePocketTool`, `AddWidgetTool`, and `RemoveWidgetTool` — that allow the agent to generate and modify Ripple UniversalSpec JSON structures for pocket workspaces. The module handles three generations of the spec format (flat widget arrays, single-pane UISpec trees, and multi-pane pane-level UISpec trees) and maintains a display-type-to-Ripple-widget mapping for backward compatibility with legacy spec fields.",
  "concepts": [
    "CreatePocketTool",
    "AddWidgetTool",
    "RemoveWidgetTool",
    "Ripple UniversalSpec",
    "UISpec",
    "multi-pane layout",
    "_DISPLAY_TYPE_TO_RIPPLE",
    "_SPAN_TO_SIZE",
    "pocket workspace",
    "backward compatibility",
    "BaseTool",
    "trust level"
  ],
  "categories": [
    "builtin tools",
    "pocket management",
    "UI spec",
    "dashboard"
  ],
  "source_docs": [
    "bfb2ad06e2988dd9"
  ],
  "backlinks": null,
  "word_count": 535,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocket.py` is the agent's interface for pocket creation and modification. A "pocket" is a structured workspace — a configured dashboard with widgets, layout, and branding — expressed as a Ripple UniversalSpec JSON document. The agent creates pockets in response to natural-language requests like "create a dashboard for tracking invoices" and returns the spec, which the frontend renders.

The module has evolved across at least two spec format generations and now supports three distinct creation paths.

## Three creation paths in CreatePocketTool

**Path 1: Flat widgets array (legacy UniversalSpec v2.0)**
The `widgets` parameter accepts a flat list of widget objects. Each widget is mapped from legacy `display.type` values to Ripple widget types using `_DISPLAY_TYPE_TO_RIPPLE`.

**Path 2: Single-pane UISpec v1.0**
The `ui` parameter accepts a nested component tree following UISpec v1.0 format. This supports richer layouts than the flat widget array.

**Path 3: Multi-pane UISpec (current)**
The `panes` + `layout` parameters enable multi-pane layouts (quad, workspace, split). Each pane gets its own UISpec node tree. When both `panes` and `layout` are provided, the `ui` and `widgets` parameters are ignored.

This layered design preserves backward compatibility — old agents or cached prompts that send `widgets` still work — while new agents can use the richer pane-level spec.

## Display type mapping

```python
_DISPLAY_TYPE_TO_RIPPLE = {
    "stats": "metric",
    "chart": "chart",
    "table": "table",
    "activity": "feed",
    "feed": "feed",
    "metric": "metric",
    "terminal": "terminal",
}
```

This mapping translates legacy `display.type` values from older pocket specs to the current Ripple widget type names. Without it, old pockets would render incorrectly or fail to parse when the Ripple frontend expects `metric` but receives `stats`.

## Span to size mapping

```python
_SPAN_TO_SIZE = {
    "col-span-1": "sm",
    "col-span-2": "md",
    ...
}
```

Similarly translates CSS-style column span classes to Ripple's named size tokens (`sm`, `md`, `lg`). This is a forward-compatibility shim for pockets created before Ripple's size system was finalized.

## AddWidgetTool and RemoveWidgetTool

`AddWidgetTool` (tool name: `add_widget`) adds a widget to an existing pocket spec identified by `pocket_id`. The `position` parameter allows placement control. `RemoveWidgetTool` (tool name: `remove_widget`) removes a widget by `widget_id` from a pocket spec.

Both tools accept `**kwargs` for forward compatibility — as the spec format evolves, new parameters can be passed without breaking the tool signature.

## Timestamp injection

```python
from datetime import UTC, datetime
```

The UTC-aware timestamp is injected into created pocket specs for versioning and cache invalidation. Using `UTC` explicitly (rather than `datetime.utcnow()`) avoids naive datetime objects that can cause timezone comparison bugs.

## Trust level

All three tools are `trust_level = "high"` — creating and modifying pocket workspaces affects what the user sees and interacts with, making it a consequential write operation.

## Known Gaps

- **No validation of pane structure**: `CreatePocketTool` accepts `panes` as raw dict input without schema validation. A malformed pane tree will not be caught at the tool layer — it will produce a broken spec that the frontend may fail to render.
- **No spec versioning**: The pocket spec does not carry an explicit format version field. When the format evolves again, there is no version discriminator to select the correct rendering path.
- **No delete pocket tool**: Pockets can be created and modified but not deleted through agent tools.