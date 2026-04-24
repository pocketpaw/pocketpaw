---
{
  "title": "PawKit Configuration Schema — Command Center Template Format",
  "summary": "PawKit is a publishable Command Center template defined as a Pydantic v2 YAML configuration. It specifies dashboard layout (panels, sections, grid columns), automated workflows (scheduled or trigger-based), user-configurable install fields, bundled skills, and external integration requirements — everything needed to ship a reusable agent workspace.",
  "concepts": [
    "PawKit",
    "PawKitConfig",
    "Command Center",
    "LayoutConfig",
    "PanelConfig",
    "WorkflowConfig",
    "WorkflowTrigger",
    "UserConfigField",
    "YAML schema",
    "Pydantic v2",
    "dashboard layout",
    "PawKitCategory"
  ],
  "categories": [
    "pawkit",
    "configuration",
    "dashboard",
    "data-models"
  ],
  "source_docs": [
    "10fb9d157b0b4548"
  ],
  "backlinks": null,
  "word_count": 541,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A PawKit is the distributable unit of a Command Center. Instead of users building dashboards from scratch, a developer publishes a PawKit YAML — a single file that describes the full workspace: what panels appear on screen, what automated workflows run in the background, what credentials the user needs to supply at install time, and what external services are required.

`pawkit.py` defines the Pydantic v2 schema for this format. It's not runtime logic — it's the shape that YAML files must conform to, plus three I/O helpers (`load_pawkit`, `save_pawkit`, `load_pawkit_from_string`).

## Layout System

The layout models (`LayoutConfig`, `SectionConfig`, `PanelConfig`) form a three-level hierarchy:

- **LayoutConfig** — top-level, declares `grid_columns` (2, 3, or 4) and an ordered list of sections
- **SectionConfig** — a titled group with a list of panels; sections are rendered top-to-bottom
- **PanelConfig** — a single visual widget

`PanelType` covers: `table`, `chart`, `kanban`, `calendar`, `metric`, `feed`, `form`, and `map`. `ChartType` adds specifics: `line`, `bar`, `pie`, `area`, `scatter`. `SpanType` controls column width: `full`, `half`, `third`, `two_thirds`.

Panels can declare `PanelAction` buttons — interactive triggers that the user can click to fire agent tasks, run workflows, or navigate to other views.

## Workflow System

`WorkflowConfig` represents an automated task the agent runs on a schedule or in response to an event. Every workflow must declare *either* a cron schedule *or* a `WorkflowTrigger` — a Pydantic `model_validator` (`_require_schedule_or_trigger`) enforces this constraint and raises a `ValueError` if both are missing.

`TriggerType` covers: `webhook`, `event`, `condition`, and `api_call`. `WorkflowOutputType` declares what the workflow produces: `notification`, `document`, `task`, `dashboard_update`, or `none`.

The validation exists because a workflow with no trigger and no schedule would never fire — it would be silently inert, which is a confusing authoring error.

## User Config Fields

`UserConfigField` models the install-time form shown to users. During PawKit installation, the system renders these fields to collect values like API keys, Slack webhook URLs, or default settings.

`UserConfigFieldType` covers: `text`, `password`, `number`, `boolean`, `select`, `multiselect`. The `password` type exists specifically for secrets — the frontend knows to mask input and the backend knows not to log or expose the value.

## PawKitConfig Root Model

`PawKitConfig` is the root model. Key fields:

```python
class PawKitConfig(BaseModel):
    name: str
    version: str
    category: PawKitCategory
    description: str
    layout: LayoutConfig
    workflows: list[WorkflowConfig] = []
    user_config: list[UserConfigField] = []
    skills: list[str] = []
    integrations: IntegrationRequirements = IntegrationRequirements()
    meta: PawKitMeta = PawKitMeta()
```

`IntegrationRequirements` lists external service dependencies (e.g., `["stripe", "github"]`), enabling the marketplace to surface compatibility warnings before install.

## YAML I/O

The three load/save helpers use a lazy `import yaml` with a guard (`_require_yaml()`). PyYAML is an optional dependency — the schema can be used in environments that only deal with dicts, never files. If YAML is missing, calling a load/save function raises an `ImportError` with a clear message rather than crashing on import.

## Known Gaps

- `load_pawkit` does not validate the YAML against the Pydantic schema before returning — parse errors from malformed YAML surface as raw Pydantic `ValidationError` rather than a friendlier PawKit-specific error.
- There is no schema version field, so future breaking changes to `PawKitConfig` cannot be detected at load time.
- The `skills` field is a plain `list[str]` of skill names with no validation that those skills actually exist.
