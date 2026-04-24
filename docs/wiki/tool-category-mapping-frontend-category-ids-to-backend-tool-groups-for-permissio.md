---
{
  "title": "Tool Category Mapping: Frontend Category IDs to Backend Tool Groups for Permission and Selection",
  "summary": "`tool_categories.py` provides two lookup tables that translate the frontend's UI category IDs (e.g., `\"google_workspace\"`) to the backend's tool group names (e.g., `[\"group:gmail\", \"group:calendar\"]`), enabling the dashboard's category-level permission checkboxes to correctly gate individual tools without hard-coding the mapping in the UI layer.",
  "concepts": [
    "tool categories",
    "CATEGORY_TO_GROUPS",
    "CATEGORY_DIRECT_TOOLS",
    "tool groups",
    "permission mapping",
    "frontend-backend bridge",
    "enterprise tools",
    "tool loader",
    "dashboard configuration"
  ],
  "categories": [
    "constants",
    "tools",
    "permissions"
  ],
  "source_docs": [
    "d1230148520bb718"
  ],
  "backlinks": null,
  "word_count": 359,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tool_categories.py` holds two dicts: `CATEGORY_TO_GROUPS` and `CATEGORY_DIRECT_TOOLS`. They exist to bridge the UX concept of "tool categories" (what a non-technical user sees in the dashboard) with the backend's granular tool group names (what the permission engine and tool loader operate on).

## `CATEGORY_TO_GROUPS`

```python
CATEGORY_TO_GROUPS: dict[str, list[str]] = {
    "google_workspace": ["group:gmail", "group:calendar", "group:drive", "group:docs"],
    "web_research":     ["group:browser", "group:search", "group:research"],
    "media":            ["group:media", "group:voice", "group:translate"],
    "social":           ["group:spotify", "group:reddit", "group:discord"],
    "execution":        ["group:shell", "group:packages"],
    "delegation":       ["group:delegation"],
    "file_system":      ["group:fs"],
    "enterprise":       [],  # handled separately
}
```

Each key is a frontend category ID that appears as a checkbox or card in the tools configuration UI. The values are backend group names that the tool loader uses to resolve which individual tools to enable. When a user enables `google_workspace`, the backend enables all tools in `group:gmail`, `group:calendar`, `group:drive`, and `group:docs`.

## `CATEGORY_DIRECT_TOOLS`

```python
CATEGORY_DIRECT_TOOLS: dict[str, list[str]] = {
    "enterprise": [
        "instinct_propose", "instinct_pending", "instinct_audit",
        "fabric_create", "fabric_query", "fabric_stats",
    ],
}
```

Enterprise tools are handled differently because they do not follow the `group:*` pattern — they are addressed by direct tool names. The `enterprise` key in `CATEGORY_TO_GROUPS` is intentionally empty (`[]`); the resolution logic checks `CATEGORY_DIRECT_TOOLS` as a fallback for any category where the group list is empty.

## Why Centralise This Mapping?

Without this file, the frontend and backend would each need to know the mapping independently, creating a synchronisation problem. Any time a new tool is added to a group, both the UI and the backend would need to be updated separately. Centralising it here means:

1. The frontend queries the backend for the mapping (or imports it via a shared schema endpoint).
2. The backend permission engine uses the same dict.
3. A new tool added to `group:gmail` automatically becomes part of `google_workspace` in the UI without touching any UI code.

## Known Gaps

- **`enterprise` group list is empty**: Enterprise tools are handled by `CATEGORY_DIRECT_TOOLS`, but the split is not obvious from `CATEGORY_TO_GROUPS` alone — a reader could miss the fallback mechanism.
- **No validation**: There is no check that the group names listed here actually correspond to registered tool groups. A typo in a group name would silently enable no tools.
