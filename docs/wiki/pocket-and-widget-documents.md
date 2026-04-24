---
{
  "title": "Pocket and Widget Documents",
  "summary": "The `Pocket` document is the primary workspace unit in PocketPaw — a named canvas that holds an ordered grid of `Widget` sub-documents, a team roster, assigned agents, and a ripple spec for graph-based automation. Widgets are embedded directly in the Pocket document and carry their own ObjectId so the frontend can address them by stable ID rather than array index.",
  "concepts": [
    "Pocket document",
    "Widget sub-document",
    "embedded documents",
    "ObjectId",
    "camelCase aliases",
    "visibility control",
    "share link",
    "ripple spec",
    "agent assignment",
    "grid layout"
  ],
  "categories": [
    "data-models",
    "pockets",
    "enterprise-cloud"
  ],
  "source_docs": [
    "1ae11b9c63859b0d"
  ],
  "backlinks": null,
  "word_count": 562,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A `Pocket` is the fundamental organizational unit visible to end users. Users create pockets to group related work — a pocket might represent a project, a workflow, or a focused data view. Each pocket contains a set of `Widget` sub-documents that render as tiles in a grid layout, each potentially backed by a live data source or an assigned AI agent.

## Widget Design

**Embedded vs. referenced** — Widgets are embedded as a `list[Widget]` inside the Pocket document rather than stored in a separate collection. This means loading a pocket loads all its widgets in one read, which is the dominant access pattern: the frontend always renders all widgets when a pocket is opened. A separate collection would require a second query and a join in the service layer.

**Widget `_id` via ObjectId** — Each widget gets its own `_id` generated from `bson.ObjectId` at construction time. The field uses a Pydantic alias so it serializes as `_id` (matching MongoDB's document structure) while being accessed as `.id` in Python. This stable ID means the frontend can issue targeted PATCH operations like "update widget `abc123`" without needing to know the widget's array index, preventing off-by-one errors when widgets are reordered.

**camelCase field aliases** — `dataSourceType`, `assignedAgent`, and `position` use camelCase aliases to match the frontend's JavaScript naming convention. `model_config = {"populate_by_name": True}` allows both the Python snake_case name and the camelCase alias to work at construction time, which is critical for server-side code that builds widgets programmatically.

**`span` field** — CSS class string (e.g., `"col-span-1"`, `"col-span-2"`) that controls widget width in the grid. Storing the CSS class directly (rather than a numeric span) means the frontend does not need to map integers to classes, but it also means invalid spans (e.g., `"col-span-99"`) will not be caught at the model layer.

**`config` and `props` dicts** — Untyped dicts for widget-type-specific configuration. `config` typically holds static setup (API keys, display options) while `props` carries runtime state. The open dict type trades type safety for extensibility: new widget types do not require schema migrations.

## Pocket-Level Fields

**`visibility` with pattern** — Constrained to `private`, `workspace`, or `public`. The default `"workspace"` means new pockets are visible to all workspace members, which matches the collaborative default. Owners can tighten access to `"private"` (only owner + `shared_with` list) or open to `"public"` via a share link.

**`share_link_token`** — A random token that forms the URL for public/view-only sharing. Storing it on the Pocket document (rather than a separate ShareLink collection) keeps the data model simple and allows revocation by nulling the token.

**`rippleSpec`** — An optional dict that stores the configuration for the Ripple automation graph associated with the pocket. The open dict type here reflects that the ripple spec format is still evolving.

**`team` and `agents`** — Both fields are `list[Any]` to accommodate either raw IDs or populated objects depending on whether the caller uses Beanie's `.fetch_links()`. This flexibility prevents type errors when mixing populated and unpopulated access patterns, but means application code must always check element types.

## Known Gaps

- `span` is an unvalidated string — invalid CSS grid values are accepted silently.
- `team` and `agents` typed as `list[Any]` lose static type safety; a `list[str | Link[User]]` pattern would be more correct.
- No index on `(workspace, owner)` — listing a user's pockets within a workspace requires a scan of the entire workspace's pockets.