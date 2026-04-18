---
{
  "title": "Fabric Package Init: Ontology Layer Public API",
  "summary": "Package init for the Fabric ontology layer that re-exports all public types (ObjectType, PropertyDef, FabricObject, FabricLink, FabricQuery) and the FabricStore. Fabric maps raw data into typed business objects with relationships so agents can reason across data.",
  "concepts": [
    "Fabric",
    "ontology layer",
    "ObjectType",
    "FabricObject",
    "FabricLink",
    "FabricStore",
    "business objects"
  ],
  "categories": [
    "fabric",
    "ontology",
    "package structure",
    "data modeling"
  ],
  "source_docs": [
    "c2392406566195bb"
  ],
  "backlinks": null,
  "word_count": 191,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Fabric Package Init: Ontology Layer Public API

## Purpose

This init file defines the public API surface for the Fabric subsystem — PocketPaw's lightweight ontology layer built on SQLite. By listing explicit `__all__` exports, it controls what `from ee.fabric import *` exposes and makes the module's contract clear.

## What Fabric Is

Fabric turns raw data into typed business objects with named relationships. Instead of agents working with arbitrary JSON blobs, they work with `FabricObject` instances that have a declared `ObjectType` (like Customer, Order, Product) and `FabricLink` connections between them. This lets agents reason about data structure — "find all orders linked to this customer" — without knowing the underlying storage format.

## Exports

- **ObjectType** — Category definition (schema for a kind of business object)
- **PropertyDef** — Property definition within an ObjectType
- **FabricObject** — An instance of an ObjectType with properties
- **FabricLink** — Directional relationship between two objects
- **FabricQuery** — Query parameters for finding objects
- **FabricStore** — Async SQLite persistence layer

## Known Gaps

- `FabricQueryResult` is not exported in `__all__` despite being defined in models.py — consumers must import it directly from `ee.fabric.models`.
