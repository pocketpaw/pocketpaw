---
{
  "title": "Pocket and Widget Models: Workspace Canvases with Embedded Widget Architecture",
  "summary": "Beanie document models for Pockets (customizable workspaces) and their embedded Widgets. Pockets are the core collaboration primitive in PocketPaw Enterprise, supporting team members, AI agents, configurable widgets with grid positioning, ripple specs, and multi-tier sharing (private, workspace, public, share links).",
  "concepts": [
    "Pocket",
    "Widget",
    "WidgetPosition",
    "embedded subdocument",
    "workspace canvas",
    "ripple spec",
    "share link",
    "visibility",
    "collaborators",
    "camelCase alias"
  ],
  "categories": [
    "Models",
    "Pockets",
    "Data Layer",
    "Collaboration"
  ],
  "source_docs": [
    "9bc6209590f9abd1"
  ],
  "backlinks": null,
  "word_count": 650,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Pocket and Widget Models: Workspace Canvases with Embedded Widget Architecture

## Purpose

A Pocket is PocketPaw's central collaboration unit — think of it as a customizable canvas or dashboard that holds widgets, team members, and AI agents. The `Pocket` model is to PocketPaw what a Channel is to Slack or a Board is to Trello, but with richer composition through embedded widgets.

## Design Decisions

### Widget as Embedded Subdocument
Widgets are embedded directly inside the Pocket document rather than being a separate collection. This is intentional:
- **Atomic updates**: Adding/removing/reordering widgets is a single document write, not a multi-document transaction.
- **Read performance**: Fetching a pocket returns all its widgets in one query — no joins or lookups.
- **Trade-off**: Large pockets with many widgets will hit MongoDB's 16MB document limit. The team accepts this because typical pockets have 5-20 widgets.

### Widget IDs via ObjectId
Each widget gets its own `_id` generated from `ObjectId()`. This is critical because the frontend addresses widgets by ID (not by array index). Without stable IDs, reordering widgets would break frontend references. The `alias="_id"` ensures JSON serialization uses `_id` (matching MongoDB convention and frontend expectations).

### camelCase Aliases
Both `Widget` and `Pocket` use Pydantic field aliases (`alias="dataSourceType"`, `alias="rippleSpec"`, etc.) with `populate_by_name=True`. This bridges the Python snake_case convention with the JavaScript camelCase convention used by the frontend. The `populate_by_name` config means both `ripple_spec` and `rippleSpec` are accepted on input.

### Visibility and Sharing Layers
Pockets have three sharing mechanisms:
1. **Visibility** (`private`/`workspace`/`public`) — broad access control
2. **Share links** (`share_link_token` + `share_link_access`) — anonymous URL-based sharing with view/comment/edit levels
3. **Collaborators** (`shared_with` list) — explicit per-user access grants

This layered approach supports different sharing patterns: team-wide workspace visibility, external stakeholder share links, and targeted collaborator invitations.

### Flexible Type Field
Unlike other models that constrain `type` with regex patterns, `Pocket.type` is a free-form string (`"custom"` default). The comment explains: "no pattern restriction — frontend sends data, deep-work, etc." This is intentional flexibility for a rapidly evolving feature set.

## Widget Fields

| Field | Type | Purpose |
|-------|------|---------|
| `id` | `str` (ObjectId) | Stable widget identifier |
| `name` | `str` | Display name |
| `type` | `str` | Widget type (`custom` default) |
| `span` | `str` | CSS grid span class |
| `dataSourceType` | `str` | Data source: `static` or dynamic |
| `config` | `dict` | Widget configuration |
| `props` | `dict` | Rendering properties |
| `data` | `Any` | Widget payload data |
| `assignedAgent` | `str | None` | Agent powering this widget |
| `position` | `WidgetPosition` | Grid row/col position |

## Pocket Fields

| Field | Type | Purpose |
|-------|------|---------|
| `workspace` | `Indexed(str)` | Parent workspace |
| `name` | `str` | Pocket name |
| `owner` | `str` | Creator user ID |
| `team` | `list[Any]` | Team member IDs or populated objects |
| `agents` | `list[Any]` | Agent IDs or populated objects |
| `widgets` | `list[Widget]` | Embedded widgets |
| `rippleSpec` | `dict | None` | Ripple automation spec |
| `visibility` | `str` | `private`, `workspace`, or `public` |
| `share_link_token` | `str | None` | Active share link token |
| `share_link_access` | `str` | Share link permission level |
| `shared_with` | `list[str]` | Explicit collaborator user IDs |

## Known Gaps

- `team` and `agents` are typed as `list[Any]` — this suggests they sometimes hold raw IDs and sometimes populated objects. This dual-use pattern makes type checking unreliable and could cause serialization bugs.
- No widget count limit enforced at the model level.
- The `WidgetPosition` model only has `row` and `col` but no `width` or `height`, despite `span` existing as a CSS class string. Grid layout logic is split between model and frontend.
- No `rippleSpec` schema validation — it is stored as a raw dict.
