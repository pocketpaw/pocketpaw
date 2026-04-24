---
{
  "title": "Fabric Module Entry Point — Ontology Layer Public API",
  "summary": "The `ee/fabric/__init__.py` consolidates the public API of the Fabric ontology layer into a single import surface, exposing both the legacy SQLite store and the newer journal-backed path introduced in Wave 3. The `__all__` list is the canonical documentation of what is stable and intended for external use.",
  "concepts": [
    "Fabric ontology",
    "FabricStore",
    "FabricJournalStore",
    "FabricProjection",
    "scope policy",
    "Wave 3",
    "event payload",
    "dual-store architecture",
    "pagination leak fix",
    "__all__"
  ],
  "categories": [
    "fabric",
    "ontology",
    "architecture",
    "module organisation",
    "scope filtering"
  ],
  "source_docs": [
    "82e1df8fc7e9cf35"
  ],
  "backlinks": null,
  "word_count": 365,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Fabric is PocketPaw's lightweight ontology layer: a system for defining typed business objects (Customer, Order, Product), creating instances of those types, linking objects together, and querying across them with scope-based visibility filtering. The `__init__.py` serves two purposes: it is the single import surface for all Fabric consumers, and it documents the two-path architecture that emerged after the Wave 3 rewrite.

## Dual-Store Architecture

The file comment captures a significant architectural moment: Fabric now has two parallel stores.

**Legacy `FabricStore`** (SQLite-backed) handles object-type definitions and object-to-object links. These are low-churn config data — types rarely change once defined, and link schemas are stable. SQLite is an appropriate fit.

**`FabricJournalStore` + `FabricProjection`** (Wave 3, `feat/fabric-journal-projection`) handles object lifecycle — creates, updates, archives. These are high-churn, per-tenant data. The journal path was introduced to fix two blockers in PR #938's attempted scope-filtering bolt-on to the legacy store: a schema migration bug where existing DBs never received the `scope` column, and a pagination leak where post-filter results paired with pre-filter totals let callers infer hidden objects exist.

Callers are expected to hold both stores simultaneously: `FabricStore` for type definitions and links, `FabricJournalStore` for object lifecycle.

## Event Payload Helpers

The module re-exports the three event payload builders (`object_created_payload`, `object_updated_payload`, `object_archived_payload`) and their action name constants. These exist so callers emitting Fabric events out-of-band (e.g., a connector syncing external data) use the same payload shape as the store itself. Building payloads by hand would create a fragile implicit contract that breaks silently when the projection's event parsing changes.

## Policy Engine

The scope policy functions (`visible`, `filter_visible`, `decide`) are exported here so they can be shared with paw-runtime's retrieval router. The comment "same containment rules everywhere so results don't diverge between Fabric and paw-runtime" is the key rationale: scope filtering must be consistent across every read path, or users would see different results depending on whether they queried through Fabric or directly through the retrieval log.

## Known Gaps

The two-store split is acknowledged as temporary — the comment notes that types and links will eventually migrate to the journal path in a follow-up slice. Until that migration, callers must coordinate two initialization paths and two `bootstrap()` calls.