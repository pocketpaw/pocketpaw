---
{
  "title": "Pocket Tool Test Suite: Widget Lifecycle, Legacy Conversion, and UISpec Contracts",
  "summary": "Comprehensive tests for `CreatePocketTool`, `AddWidgetTool`, and `RemoveWidgetTool` — the three built-in tools that let AI agents build and mutate dashboard \"pockets\" in PocketPaw. The suite enforces spec versioning (v1.0 UISpec vs v2.0 widget array), backward compatibility of the `name→title` alias, and structural contracts on every tool's JSON output.",
  "concepts": [
    "CreatePocketTool",
    "AddWidgetTool",
    "RemoveWidgetTool",
    "pocket spec",
    "UISpec v1.0",
    "widget array v2.0",
    "legacy widget conversion",
    "mutation envelope",
    "pocket_event",
    "widget ID generation",
    "dashboard layout",
    "tool trust level",
    "multi-pane layout",
    "backward compatibility"
  ],
  "categories": [
    "testing",
    "dashboard tools",
    "pocket spec",
    "built-in tools",
    "test"
  ],
  "source_docs": [
    "fad0186da06a389d"
  ],
  "backlinks": null,
  "word_count": 540,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's "pocket" concept is a structured, AI-generated dashboard: a titled container of widgets (metrics, charts, tables, feeds) that the runtime renders in the UI. Three tools in `pocketpaw.tools.builtin.pocket` drive this: `CreatePocketTool` builds a new pocket spec, `AddWidgetTool` appends a widget by mutation, and `RemoveWidgetTool` removes one. This test file is the contract surface for all three.

## Why These Tests Exist

Without strict output-shape tests an AI model could emit a malformed spec and the frontend would silently render nothing or crash. The test suite acts as a compile-time guard: if the tool's JSON serialization drifts, a CI failure tells you before it hits a live session.

## Output Format: Dual Versioning

`CreatePocketTool` produces two distinct spec shapes:

- **v2.0 widget array** — the default path when `widgets` are passed. Has a `dashboard_layout` grid, `display.columns`, and a top-level `widgets` array.
- **v1.0 UISpec** — activated when a non-empty `ui` dict is provided. Carries a free-form `ui` component tree; `widgets` is absent. A provided `panes` dict triggers the multi-pane layout variant (also v1.0).

`test_ui_takes_precedence_over_widgets` and `test_empty_ui_falls_back_to_widgets` nail down the precedence logic: if `ui={}` the system treats it as absent and falls back to widget mode. This prevents an empty `ui` dict from accidentally suppressing a valid widget list.

## Lifecycle and Metadata Assertions

Every created pocket gets an `ai-`-prefixed lifecycle ID (`spec["lifecycle"]["id"]`), a `persistent` lifecycle type, and a full metadata block (`category`, `color`, `pocket_version`, `created_at`). The tests validate each field individually so regressions in the factory are immediately visible.

## Legacy Widget Conversion

The codebase supports an older widget schema (`display.type`, `stats`, `bars`, `rows`, `feedItems`). `_convert_legacy_widget` is the adapter. Tests cover all legacy variants:

- `stats` with multiple entries → multiple metric widgets (IDs get `-s0`, `-s1` suffixes to stay unique)
- `chart` → chart widget with `props.type` from `chartType`
- `table` → table widget with `columns` from `headers`
- `feed` / `activity` → feed widget

The multi-stat expansion is notable: a single legacy widget with 2 stats becomes 2 distinct metric widgets. Without the ID-suffix rule (`-s0`, `-s1`) IDs would collide and the frontend could mis-render.

## AddWidgetTool and RemoveWidgetTool

Both tools emit a `pocket_event: mutation` envelope rather than a full spec. `AddWidgetTool` auto-generates a scoped widget ID (`{pocket_id}-w{n}`), so the frontend can place the widget without a round-trip. `RemoveWidgetTool` emits the widget ID to remove. Tests confirm the mutation action strings (`add_widget`, `remove_widget`) and the human-readable confirmation message embedded after the JSON block.

## Tool Metadata

`TestToolMetadata` validates the tool protocol contract: correct `name` strings (`create_pocket`, `add_widget`, `remove_widget`), `trust_level == "standard"`, and the JSON Schema `required` arrays. `title`, `description`, and `category` are required on create; `widgets` is optional (because `ui` or `panes` can replace it). The `ui` property must be present in the schema even when not required.

## Helper Utilities

`_extract_spec(result)` and `_extract_mutation(result)` parse the tool's dual-part output: JSON block, two newlines, then a human message. Asserting `pocket_event == "created"` or `"mutation"` inside the helper means a test failure will report the actual event type, making regressions easier to diagnose.

## Known Gaps

No negative tests cover invalid widget types (e.g., an unknown `type` value) — the suite assumes the tool or frontend validates that. There are no rate-limit or concurrency tests for the mutation tools.
