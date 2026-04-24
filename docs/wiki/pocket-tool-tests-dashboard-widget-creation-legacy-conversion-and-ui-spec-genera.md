---
{
  "title": "Pocket Tool Tests: Dashboard Widget Creation, Legacy Conversion, and UI Spec Generation",
  "summary": "The pocket tool system lets AI agents create and modify PocketPaw dashboard \"pockets\" — widget-based layouts containing metrics, charts, tables, and feeds. These tests validate the `create_pocket`, `add_widget`, and `remove_widget` tools, including backward-compatible legacy widget conversion, the `ui` parameter for pre-built specs, and the JSON Schema definitions used by LLM providers.",
  "concepts": [
    "pocket tool",
    "create_pocket",
    "add_widget",
    "remove_widget",
    "dashboard widget",
    "legacy widget conversion",
    "UI spec",
    "metric widget",
    "chart widget",
    "table widget",
    "mutation pattern",
    "trust level"
  ],
  "categories": [
    "testing",
    "dashboard",
    "tool system",
    "test"
  ],
  "source_docs": [
    "fad0186da06a389d"
  ],
  "backlinks": null,
  "word_count": 539,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's dashboard is composed of "pockets" — self-contained panels created by agents at runtime. The `pocket` tool family allows LLMs to compose these panels declaratively. The test suite protects two generations of the API: the current widget-based API and a legacy stats/chart/table format that must remain supported for backward compatibility.

## create_pocket: Universal Spec Contract

`TestCreatePocketTool` validates that `create_pocket` returns a spec conforming to the universal Pocket Spec format:

- **Universal spec returned**: the result contains a parseable JSON spec with `lifecycle` and `metadata` sections.
- **Lifecycle present**: governs how the pocket refreshes and when it expires.
- **Metadata present**: title, icon, and other display fields.
- **Dashboard layout**: a `layout` section defines widget arrangement.
- **Widget types**: metric, chart, and table widgets each render correctly.
- **Widget IDs generated**: every widget must have a unique ID for the frontend to track state — IDs are auto-generated if not provided.
- **Legacy `name` param**: the old `name` parameter maps to `title`, supporting older agent prompts.
- **Multiple widget types in one pocket**: a pocket can mix metrics, charts, and tables.
- **Result message**: the tool returns a human-readable confirmation alongside the spec.

```python
async def test_metric_widget(create_tool):
    result = await create_tool.execute(
        title="Revenue",
        widgets=[{"type": "metric", "value": 42, "label": "MRR"}]
    )
    spec = _extract_spec(result)
    assert any(w["type"] == "metric" for w in spec["layout"]["widgets"])
```

## Legacy Widget Conversion

`TestLegacyWidgetConversion` and `TestConvertLegacyWidget` cover the migration path from the old widget format:

- **stats with multiple entries** → multiple metric widgets (one per stat).
- **chart** → chart widget (type preserved).
- **table** → table widget.
- **feed/activity** → feed widget (renamed).
- **terminal** → passthrough (type unchanged).

This conversion layer exists because early PocketPaw agents were trained on the legacy format. Removing it would silently break all pockets created before the format migration.

## UI Spec Parameter

`TestCreatePocketUISpec` tests the `ui` parameter, which accepts a pre-built spec directly:

- **`ui` param produces v1 spec**: the spec format version is set correctly.
- **`ui` takes precedence over `widgets`**: when both are provided, `ui` wins — preventing accidental merging.
- **Empty `ui` falls back to `widgets`**: an empty `ui` value is treated as absent.
- **Multi-pane spec**: the `ui` param supports multi-pane layout structures.

## add_widget and remove_widget

`TestAddWidgetTool` and `TestRemoveWidgetTool` validate the mutation tools:

- **add_widget**: returns a mutation object describing the widget to insert, optionally with a position.
- **remove_widget**: returns a mutation with the widget ID to remove.
- **ID generation**: `add_widget` generates a widget ID if not provided.
- **Mutation messages**: both tools return confirmation strings.

Mutations are returned as structured JSON rather than applied directly because the dashboard frontend manages widget state — the backend emits intents, not DOM operations.

## Tool Metadata and Schema

`TestToolMetadata` validates the tool registry contract:

- All three tools have `standard` trust level.
- `create_pocket` requires `title` and `widgets` as parameters.
- `add_widget` and `remove_widget` have the expected parameter schemas.

## Known Gaps

- No test for widget validation — passing an unknown widget type is untested.
- No test for the `lifecycle` section fields (refresh interval, expiry time).
- Multi-pane spec tests only check structure, not rendering correctness.
- `remove_widget` does not test what happens if the widget ID does not exist.