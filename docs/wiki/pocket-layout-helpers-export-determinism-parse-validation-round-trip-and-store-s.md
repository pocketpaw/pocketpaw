---
{
  "title": "Pocket Layout Helpers: Export Determinism, Parse Validation, Round-Trip, and Store Scoping Tests",
  "summary": "This unit test suite covers the pure Python helpers in `ee.cloud.pockets.layouts` — specifically `export_layout_yaml` (deterministic YAML serialization), `parse_layout_yaml` (safe YAML parsing with input validation), and `UserTemplateStore` (in-process template store with workspace scoping). No FastAPI, Beanie, or MongoDB is involved.",
  "concepts": [
    "export_layout_yaml",
    "parse_layout_yaml",
    "YAML determinism",
    "PocketLayout",
    "UserTemplateStore",
    "workspace scoping",
    "round-trip",
    "kind validation",
    "spec validation",
    "ripple_spec fallback"
  ],
  "categories": [
    "testing",
    "pocket layouts",
    "YAML parsing",
    "data serialization",
    "test"
  ],
  "source_docs": [
    "74248f0847f5d46b"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.cloud.pockets.layouts` module contains the low-level helpers that the pocket layout routes depend on. These helpers are pure Python functions and an in-memory store, making them testable in isolation without any infrastructure. This test file pins their contracts before the routes become the primary test surface.

## Export Determinism (`TestExportDeterminism`)

**`test_same_input_yields_byte_identical_yaml_excluding_timestamp`** — Calling `export_layout_yaml` twice with identical inputs produces byte-identical YAML (excluding the `exportedAt` timestamp line). This matters for caching, diffing, and deduplication: if the export were non-deterministic (e.g., due to random dict ordering), two exports of the same pocket would look different and could not be compared.

The test strips `exportedAt` lines from both outputs and compares the remainder. The timestamp is excluded because it inherently changes between calls, but everything else — key ordering, widget serialization, layout values — must be stable.

## Export Shape (`TestExportShape`)

**`test_yaml_carries_required_top_level_keys`** — The YAML must contain `apiVersion: pocketpaw.io/v1`, `kind: PocketLayout`, `name:`, and `sourcePocketId:`. These fields are required for the import parser to recognize and validate the document. Missing any of them would cause silent failures when the YAML is imported into another instance.

**`test_empty_ripple_spec_falls_back_to_widgets_mirror`** — When `ripple_spec=None`, the exporter falls back to serializing the `widgets` list directly under `spec`. This supports older pockets that don't yet have a `ripple_spec` — they can still be exported. `parse_layout_yaml` on the fallback output must return a dict with a `widgets` key.

## Parse Validation (`TestParseValidation`)

**`test_malformed_yaml_raises_valueerror`** — Structurally invalid YAML raises `ValueError` with "Invalid YAML" in the message. The error is safe to surface to the caller (not an internal YAML parser exception) because `ValueError` is a domain-level error, not an implementation detail.

**`test_missing_spec_raises_valueerror`** — Valid YAML that lacks a `spec:` block raises `ValueError` with "spec" in the message. This catches YAML documents that are syntactically valid but semantically incomplete.

**`test_unknown_kind_is_rejected`** — A document with `kind: OtherThing` raises `ValueError` with "Unsupported template kind". This prevents accidentally importing the wrong document type (e.g., a Kubernetes manifest).

**`test_list_root_is_rejected`** — A YAML document whose root is a list (rather than a mapping) raises `ValueError` with "mapping" in the message. YAML lists are valid YAML but not valid PocketLayout documents. Without this check, the parser would try to access `doc["spec"]` on a list and produce a confusing `TypeError`.

## Round-Trip (`TestRoundTrip`)

**`test_export_then_parse_recovers_the_spec`** — `export_layout_yaml` followed by `parse_layout_yaml` returns the original `ripple_spec` dict unchanged. This is the core correctness guarantee: the import/export cycle is lossless.

## Store Scoping (`TestStoreScoping`)

**`test_templates_do_not_leak_between_workspaces`** — A template saved under workspace `"ws-alpha"` must not appear in `"ws-beta"`'s template list. This is a fundamental multi-tenancy requirement. The `UserTemplateStore` is an in-process store (currently a dict, likely to be replaced with a database), and the test pins that even the simplest implementation must scope by workspace.

## Known Gaps

- **No test for template update or delete** — The store tests only cover create + list + scoping. Update and delete paths are not covered at the unit level.
- **`export_layout_yaml` does not validate the `ripple_spec` schema** — The function serializes whatever dict is passed in. Malformed specs (e.g., missing `widgets` key) would serialize without error and fail only at parse time.
