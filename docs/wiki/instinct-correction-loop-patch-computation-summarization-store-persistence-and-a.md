---
{
  "title": "Instinct Correction Loop: Patch Computation, Summarization, Store Persistence, and Approve Endpoint Tests",
  "summary": "Covers the \"Correction Loop\" — the subsystem that detects when a user edits an AI-proposed action during approval, records what changed as structured patches, and surfaces those corrections for soul memory promotion. Tests span the pure diff functions, the `InstinctStore` correction persistence layer, and the `/approve` HTTP endpoint behavior.",
  "concepts": [
    "CorrectionPatch",
    "compute_patches",
    "summarize_correction",
    "correction store",
    "approve endpoint",
    "field diff",
    "dotted path",
    "audit entry",
    "soul memory promotion",
    "correction loop"
  ],
  "categories": [
    "instinct",
    "correction",
    "testing",
    "enterprise",
    "test"
  ],
  "source_docs": [
    "7897a89865d235db"
  ],
  "backlinks": null,
  "word_count": 586,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Corrections Are Tracked

When a user approves an action but changes its parameters first (e.g., changes `tone` from `formal` to `casual`, or reduces `discount_pct` from 20 to 15), those edits are signals: the agent proposed something slightly wrong, and a human corrected it. Without recording these corrections, the soul never learns. The Correction Loop makes human edits machine-readable.

## TestComputePatches: Field-Level Diff Logic

`compute_patches(original, edited)` produces a list of `CorrectionPatch` objects — each with a `path`, `before`, and `after` value.

Key behaviors verified:

- **Identical actions produce no patches** — zero-diff is the common case (most approvals are unedited); it must not produce false noise.
- **Enum fields normalize to string values** — `ActionPriority.HIGH` is stored as `"high"`, not as the enum object. This prevents raw enum serialization from appearing in patches.
- **Dotted path notation** — nested fields like `parameters.discount_pct` are represented as dotted paths rather than nested dicts, making patches human-readable and sortable.
- **Context field is ignored** — the `context` field carries reasoning metadata (object IDs, metrics) that is not part of the user-editable action content. Including it in diffs would generate spurious patches on every call.
- **Parameter addition and removal** — adding a new parameter key or removing an existing one both produce patches.

## TestSummarizeCorrection: Human-Readable Summary

`summarize_correction(correction)` produces a one-line string for use in soul memories and audit entries.

- **Zero patches** → `"Approved without edits"` — unedited approvals must not be summarized as corrections.
- **Up to five patches** — field names are listed individually: `"Changed: tone, discount_pct"`.
- **More than five patches** — first five are listed with an overflow counter: `"Changed: a, b, c, d, e and 2 more"`. This prevents extremely long summary strings when an action has many parameters.

## TestCorrectionStore: Persistence

Five store-level tests cover the `record_correction` and query methods:

- **Persists the row** — a `Correction` written to the store is retrievable.
- **Writes audit entry** — every correction also writes an `AuditEntry` so corrections appear in the audit trail alongside proposals, approvals, and executions.
- **Filters by pocket** — `get_corrections_for_pocket("pocket-1")` returns only corrections for that pocket, not all corrections in the database.
- **Orders newest first** — the correction history view shows most recent corrections first.
- **Count by path** — `count_corrections_by_path(pocket_id, path)` returns the number of times a specific field path has been corrected. This count drives the 3x procedural promotion heuristic in the `CorrectionSoulBridge`.

## TestApproveEndpoint: HTTP Behavior

The `/instinct/actions/{id}/approve` endpoint accepts an optional body with field overrides. Three cases:

**Approve with no edits** — body matches the original action exactly; no `Correction` is stored. The test verifies that `get_corrections_for_pocket()` returns an empty list.

**Approve with edits** — body differs in `title` or `parameters`; a `Correction` is stored with the correct patches. The test verifies patch count and field values.

**Equal body** — a body that is structurally identical to the original (same values, different serialization path) must be treated as unchanged. This prevents spurious corrections from JSON round-trip differences.

**Unknown action** — a 404 is returned for an unrecognized action ID rather than a 500.

## TestCorrectionsEndpoint

- `GET /instinct/corrections?pocket_id=x` returns corrections filtered by pocket.
- `GET /instinct/corrections` without a `pocket_id` returns 400, enforcing that corrections are always queried in the context of a specific pocket.

## Known Gaps

The `context` field exclusion is hardcoded in `compute_patches`. If other fields need to be excluded in the future (e.g., system-generated timestamps), the exclusion list would need to grow. No mechanism for configuring excluded fields is currently present.