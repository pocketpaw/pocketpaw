---
{
  "title": "PawKit Pydantic Models: Dashboard Configuration Schema",
  "summary": "The `kits.models` module defines the full Pydantic schema hierarchy for PawKit configurations — from individual metric cards up to the root `PawKitConfig` that a YAML file is parsed into. It also defines `InstalledKit`, which tracks activation status and user-provided configuration values at runtime.",
  "concepts": [
    "PawKitConfig",
    "PanelConfig",
    "MetricItem",
    "SectionConfig",
    "LayoutConfig",
    "WorkflowConfig",
    "UserConfigField",
    "InstalledKit",
    "Pydantic",
    "extra=allow",
    "dashboard schema"
  ],
  "categories": [
    "kits",
    "models",
    "dashboard",
    "schema"
  ],
  "source_docs": [
    "a74479d551005e29"
  ],
  "backlinks": null,
  "word_count": 413,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/kits/models.py` is the schema layer for PawKits. Every kit a user installs is represented as a `PawKitConfig` parsed from YAML, and its installed state is tracked as an `InstalledKit`. The models use Pydantic with `extra="allow"` on panel configs to support type-specific fields without requiring a union of every panel type.

## Model Hierarchy

```
PawKitConfig
├── PawKitMeta          — display metadata (name, author, icon, category)
├── LayoutConfig        — top-level grid definition
│   └── [SectionConfig] — titled sections
│       └── [PanelConfig] — individual widget panels
│           └── [MetricItem] — metric cards (for metrics-row panels)
├── [WorkflowConfig]    — scheduled/trigger-based workflows
└── [UserConfigField]   — install-time user prompts
```

## PanelConfig: Open Schema for Widget Types

```python
class PanelConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str  # "metrics-row" | "table" | "kanban" | "feed" | "chart"
```

The `extra="allow"` configuration is the key design decision. Rather than defining a union type for every possible panel widget, `PanelConfig` accepts any additional fields. This makes adding new panel types a frontend concern — new field names flow through without a schema change. The downside is reduced validation at parse time.

## WorkflowConfig: Automation Layer

```python
class WorkflowConfig(BaseModel):
    id: str
    name: str
    trigger: str      # "schedule" | "webhook" | "event"
    schedule: str | None  # cron expression
    agent_tool: str   # tool to invoke
    output_key: str   # where to store result
```

Workflows tie the dashboard to live data — a scheduled workflow runs an agent tool and stores the result under `output_key`, which panels reference via `source: "workflow:<id>"`.

## MetricItem: Data Binding

```python
class MetricItem(BaseModel):
    label: str
    source: str   # "workflow:<id>" or "api:<endpoint>"
    field: str    # JSON path into the source response
    format: Literal["number", "currency", "percent", "text"]
    trend: bool   # show delta arrow
```

The `source` field uses a URI-like convention to decouple the metric from its data origin. Panels reference workflows by ID, not by implementation.

## InstalledKit: Runtime State

```python
class InstalledKit(BaseModel):
    kit_id: str
    config: PawKitConfig
    user_values: dict[str, Any]  # filled from UserConfigField prompts
    active: bool
    installed_at: datetime
```

`user_values` captures what the user typed during the install wizard (e.g., a GitHub repo name). The `active` flag controls whether the kit renders in the UI.

## Known Gaps

- **No panel type enum**: The `type` field on `PanelConfig` is a plain `str`. Invalid panel types silently pass validation and only fail at render time in the frontend.
- **No schema migration**: `PawKitConfig` has no version field. If the schema evolves, old YAML files on disk may fail to parse.