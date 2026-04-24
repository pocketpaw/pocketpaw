---
{
  "title": "Ripple Spec Normalizer: Canonicalising AI-Generated Pocket Layouts Before Persistence",
  "summary": "The ripple normalizer is a single-function module that accepts the free-form dict output of agent-generated pocket specs and canonicalises it into a consistent envelope shape before it is written to MongoDB. It handles three distinct spec variants (multi-pane, UISpec v1.0, and flat widget lists) with minimal transformation, and generates stable widget IDs when they are missing to ensure idempotent downstream rendering.",
  "concepts": [
    "ripple spec",
    "normalisation",
    "AI-generated pockets",
    "widget IDs",
    "envelope fields",
    "lifecycle",
    "UISpec",
    "multi-pane",
    "flat widgets",
    "token_hex",
    "idempotent rendering",
    "spec variants"
  ],
  "categories": [
    "pockets",
    "ripple",
    "normalisation",
    "EE cloud"
  ],
  "source_docs": [
    "e287b97ee54ce7b5"
  ],
  "backlinks": null,
  "word_count": 471,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/ripple_normalizer.py` exists because AI-generated ripple specs are structurally diverse. Different agent pipelines produce layouts with different top-level keys, different ways of expressing a pocket name, and widgets that may or may not carry unique IDs. Without normalisation, the frontend would have to handle all these variants at render time — or worse, render incorrect layouts when expected envelope fields are absent.

## The Normalisation Problem

AI agents generating pocket specs may produce any of several shapes:

- `{"panes": {...}, "title": "..." }` — multi-pane layout
- `{"ui": {"type": "tabs", ...}, "title": "..."}` — UISpec v1.0
- `{"widgets": [{"name": "Revenue"}, ...]}` — flat widget list
- `{"name": "...", "description": "..."}` — minimal spec with no layout

Each needs different treatment, but all need the same envelope fields for the frontend to function correctly.

## Envelope Fields

Every normalised spec receives a consistent set of top-level envelope fields:

```python
envelope = {
    "lifecycle": spec.get("lifecycle") or {"type": "persistent", "id": pocket_id},
    "title": name or spec.get("title"),
    "name": name or spec.get("name"),
    "color": color,
    "metadata": {
        "category": ...,
        "color": color,
        **meta,
    },
}
```

The `lifecycle.id` is either taken from the spec or generated as `pocket-{4-hex-byte-token}`. Using `secrets.token_hex(4)` for ID generation ensures uniqueness without requiring a database round-trip, and the `pocket-` prefix makes IDs visually recognisable in logs.

## Variant Routing

The function checks variants in priority order:

1. **Multi-pane** (`spec.get("panes")`): Pass through with envelope and version. The pane structure is complex and not transformed.
2. **UISpec v1.0** (`spec.get("ui")` with a `type` key): Same pass-through treatment.
3. **Flat widgets** (`spec.get("widgets")`): Augment each widget with an ID if missing, and a `title` if missing. Also sets `version`, `intent`, `display`, and `dashboard_layout` defaults.
4. **Fallback** (no recognised structure): Return the spec with envelope only.

## Widget ID Generation

For flat widget specs, widget IDs are generated as `{pocket_id}-w{index}` if absent:

```python
if not w.get("id"):
    w["id"] = f"{pocket_id}-w{i}"
```

Stable IDs matter because the frontend uses widget IDs for DOM reconciliation during layout updates. Without IDs, every re-render would destroy and recreate all widgets, losing widget-local state (e.g., scroll position, expanded/collapsed state).

## Defensive Input Handling

The function handles several defensive cases:

- Returns `None` for a falsy or non-dict input, allowing callers to check for `None` before persisting.
- Skips non-dict entries in the widgets list (`if not isinstance(w, dict): continue`).
- Falls back through a chain of possible name fields (`title`, `name`, `lifecycle.name`) before defaulting to `None`.

## Known Gaps

- **No schema validation**: The normaliser does not validate that the spec is semantically correct, only that it has the expected envelope. An agent could produce a widgets list with invalid widget configs and they would be persisted as-is.
- **No version migration**: If the ripple spec format evolves, there is no version discriminator to apply different normalisation paths for old vs new specs in the database.