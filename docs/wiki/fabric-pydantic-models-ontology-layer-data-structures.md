---
{
  "title": "Fabric Pydantic Models — Ontology Layer Data Structures",
  "summary": "This module defines the Pydantic models that represent the Fabric ontology layer's core entities: property definitions, object types, object instances, directional links between objects, query parameters, and query results. The `_gen_id` function produces time-ordered, prefix-tagged identifiers that balance human readability with sortability.",
  "concepts": [
    "FabricObject",
    "ObjectType",
    "FabricLink",
    "PropertyDef",
    "FabricQuery",
    "time-ordered ID",
    "ontology",
    "source deduplication",
    "directional link",
    "schemaless properties"
  ],
  "categories": [
    "fabric",
    "data models",
    "Pydantic",
    "ontology",
    "identifier generation"
  ],
  "source_docs": [
    "03930335033bcae9"
  ],
  "backlinks": null,
  "word_count": 429,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Fabric's type system mirrors a simplified ontology: you define categories of things (`ObjectType`), create instances (`FabricObject`), and connect them directionally (`FabricLink`). These three models form the foundation. `PropertyDef`, `FabricQuery`, and `FabricQueryResult` complete the layer.

## Identifier Generation

`_gen_id(prefix)` generates IDs of the form `ot-1a2b3c4d-ab12` where:
- The prefix tags the entity type (`ot` for ObjectType, `obj` for FabricObject, `lnk` for FabricLink).
- The timestamp component (`hex(int(time.time() * 1000))`) makes IDs time-ordered, which improves index locality in SQLite's B-tree.
- The 4-character random suffix provides collision resistance within the same millisecond.

This pattern is a lightweight alternative to UUID v4: human-readable in logs, sortable, and collision-resistant for the expected write rates of an ontology store.

## PropertyDef

`PropertyDef` defines a schema for a property on an `ObjectType`. It supports five primitive types (`string`, `number`, `boolean`, `date`, `enum`), with `enum_values` only meaningful when `type == "enum"`. The `default` and `required` fields let the UI render appropriate form controls without additional metadata.

## ObjectType

`ObjectType` is the schema for a category of business objects — analogous to a table definition. The `icon` and `color` fields exist for the PocketPaw UI: the Objects panel renders each type with its icon and color in the sidebar.

## FabricObject

`FabricObject` is a schemaless instance: `properties` is a free-form `dict[str, Any]`. The type system is enforced at the application layer (via `PropertyDef`), not at the model level — this keeps the model simple and lets properties evolve without schema migrations.

`source_connector` and `source_id` record the origin of objects synced from external systems. These fields let Fabric deduplicate: if a connector tries to sync the same Salesforce contact twice, the second sync can detect the existing record via `source_connector + source_id` and update rather than create.

## FabricLink

`FabricLink` is directional: `from_object_id` → `to_object_id` via `link_type`. The `link_type` string (e.g., `"has_orders"`, `"belongs_to"`) is application-defined and has no schema constraint here. Properties on links allow metadata about the relationship itself (e.g., purchase date on a `"purchased"` link).

## FabricQuery

`FabricQuery` encodes the query surface: filter by type name or ID, filter by property values, filter by linked-to object and link type. `limit` and `offset` provide pagination. The `linked_to` + `link_type` fields enable graph traversal queries: "give me all objects of type Order linked to this Customer via has_orders".

## Known Gaps

The `_gen_id` function uses `random.choices` from Python's standard library, which is not cryptographically random. For entity IDs in a non-security-sensitive store this is acceptable. However, if Fabric IDs are ever exposed in a security-sensitive context (e.g., used as tokens), this should be replaced with `secrets.token_hex`.