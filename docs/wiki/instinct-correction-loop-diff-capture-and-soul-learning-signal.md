---
{
  "title": "Instinct Correction Loop — Diff Capture and Soul Learning Signal",
  "summary": "Defines the data types and diff logic that record what a human changed when editing an agent's proposed action before approving it. These structured diffs are consumed by soul-protocol to bias future proposals toward the human's preferences.",
  "concepts": [
    "CorrectionPatch",
    "Correction",
    "compute_patches",
    "field-level diff",
    "soul learning signal",
    "enum normalization",
    "episodic memory",
    "procedural memory",
    "human edit capture",
    "action approval",
    "recall key"
  ],
  "categories": [
    "instinct engine",
    "correction loop",
    "soul protocol integration",
    "enterprise edition"
  ],
  "source_docs": [
    "5d2e2503da1cc08d"
  ],
  "backlinks": null,
  "word_count": 452,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

When an agent proposes an action and a human edits it before approving, something important has happened: the agent was wrong in a specific, structured way. `correction.py` captures that wrongness precisely rather than just overwriting the proposal, turning a one-off edit into a durable learning signal.

## Core Types

### CorrectionPatch

A single field-level change: `path` (the field name), `before` (the agent's value), `after` (the human's value). Patches are granular by design — if a human changes both `title` and `priority`, two patches are created so each field's edit history can be tracked independently.

### Correction

The full record of a human edit event. It holds:
- `id` — prefixed with `"cor_"` for traceability in logs and foreign keys
- `action_id` / `pocket_id` — scope the correction to a specific action and workspace
- `actor` — who approved, needed for per-user learning eventually
- `patches` — the list of `CorrectionPatch` objects
- `context_summary` — a short string used as the soul recall key
- `action_title` — denormalized for display without a join
- `created_at` — timestamp for correction history queries

## diff Logic — compute_patches

`compute_patches(before, after)` compares two `Action` snapshots across the `_CORRECTABLE_SCALAR_FIELDS` tuple: `title`, `description`, `recommendation`, `category`, `priority`. Only these five fields are diffed because they are the semantic fields an agent generates from reasoning. Fields like `id`, `status`, `created_at`, or `parameters` are either immutable identifiers or execution-side state that humans do not meaningfully edit in the approval flow.

The `_normalize(value)` helper converts enums to their `.value` strings before comparison. Without this, comparing `ActionPriority.HIGH` (an enum member) to `"high"` (a string) would generate a spurious patch even when the value is semantically identical after a round-trip through the HTTP layer.

## summarize_correction

`summarize_correction(action, patches)` produces a short natural-language string like `"Changed priority from medium to high on action: Fix overdue invoices"`. This string becomes the `context_summary` stored on the `Correction` and is passed directly to `soul.observe()` as the interaction text. The soul's BM25 index tokenizes it, so future `soul.recall("priority")` or `soul.recall("invoices")` calls surface this correction in relevant contexts.

## Connection to Soul Protocol

Corrections are consumed by `CorrectionSoulBridge`, which calls `soul.observe()` for every correction (episodic tier, importance 5) and `soul.remember()` when the same `path` is edited three or more times (procedural tier, importance 7). The threshold-based promotion prevents noisy one-off edits from polluting procedural memory while still capturing genuine behavioral preferences.

## Known Gaps

- `_CORRECTABLE_SCALAR_FIELDS` is a module-level constant tuple; adding new correctable fields requires editing this file directly rather than deriving them from the `Action` schema.
- List-valued fields (`parameters`, `context.object_ids`) are not diffed — only scalar fields are compared. Deep diffs of nested structures would require a recursive patch algorithm that is not yet implemented.