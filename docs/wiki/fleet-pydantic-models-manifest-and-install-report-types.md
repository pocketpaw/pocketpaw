---
{
  "title": "Fleet Pydantic Models — Manifest and Install Report Types",
  "summary": "This module defines the Pydantic models for the Fleet subsystem: the `FleetTemplate` manifest (what to install), `FleetConnector` (a single connector registration), `FleetInstallStep` (one step's outcome), and `FleetInstallReport` (the full install result with convenience methods). These models are thin data containers — no new runtime concepts, just structured representations of the install lifecycle.",
  "concepts": [
    "FleetTemplate",
    "FleetConnector",
    "FleetInstallStep",
    "FleetInstallReport",
    "optional connector",
    "Literal type",
    "succeeded method",
    "failed_steps",
    "soul_template",
    "pocket_name",
    "manifest model"
  ],
  "categories": [
    "fleet",
    "data models",
    "Pydantic",
    "installation",
    "manifest"
  ],
  "source_docs": [
    "d12ff0b1509b5bff"
  ],
  "backlinks": null,
  "word_count": 417,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## FleetTemplate

`FleetTemplate` is the YAML manifest parsed into a typed Python object. Its fields cover the four things a fleet installs:

- `soul_template` (required) — the bundled soul template name (`arrow`, `flash`, `cyborg`, `analyst`). This drives `SoulFactory.from_template`.
- `soul_name` — optional override of the template's default name.
- `pocket_name` (required) — the pocket created at install time.
- `pocket_widgets` — pre-seeded widget configuration for the pocket.
- `connectors` — a list of `FleetConnector` instances.
- `scopes` — scope tags assigned to the soul and events.
- `metadata` — free-form dict for custom installer extensions.

The `display_name` and `description` fields are for the UI fleet browser — they have no effect on the install process itself.

## FleetConnector

`FleetConnector` records one connector to register. The `optional: bool = False` field is important: when `True`, a missing connector module causes the install step to emit `status="skipped"` rather than `status="failed"`. This allows fleet templates to include connectors that are only available in certain PocketPaw editions without blocking the entire install on an unavailable module.

## FleetInstallStep

`FleetInstallStep` captures the outcome of one step in the install pipeline. The `status` field uses a `Literal["succeeded", "skipped", "failed"]` type rather than an enum, which keeps it JSON-serializable without a custom encoder. `duration_ms` allows the UI to show a progress timeline and identify slow steps.

```python
class FleetInstallStep(BaseModel):
    name: str
    status: Literal["succeeded", "skipped", "failed"]
    detail: str = ""
    duration_ms: int = 0
```

## FleetInstallReport

`FleetInstallReport` aggregates all steps and adds two convenience methods:

- `succeeded()` — returns `True` if no step has `status="failed"`. Skipped steps are not failures.
- `failed_steps()` — returns only the failed steps, used by the UI to show an error summary without iterating the full list.

The `soul_id` and `pocket_id` fields record the IDs of the created soul and pocket so the installer can return them to the caller without the caller needing to re-query.

## Design Principle

The module comment captures the intent precisely: "A fleet is a thin orchestration over primitives that already exist (soul template, pocket, connectors, scope). No new runtime concepts; the manifest just names them in one place." The models enforce this by having no behavior beyond validation and the two utility methods on `FleetInstallReport`.

## Known Gaps

- `pocket_widgets` is `list[dict[str, Any]]` — fully schemaless. A future improvement would define a typed widget manifest model so the YAML can be validated against a known shape at load time rather than failing at runtime when the pocket service rejects an unknown widget config.