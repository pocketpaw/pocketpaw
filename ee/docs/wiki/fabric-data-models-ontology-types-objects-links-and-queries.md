---
{
  "title": "Fabric Data Models: Ontology Types, Objects, Links, and Queries",
  "summary": "Pydantic models defining the Fabric ontology schema — PropertyDef for type-level property definitions, ObjectType for business object categories, FabricObject for instances, FabricLink for directional relationships, and FabricQuery/FabricQueryResult for querying. All IDs are generated client-side with time-based prefixed strings.",
  "concepts": [
    "PropertyDef",
    "ObjectType",
    "FabricObject",
    "FabricLink",
    "FabricQuery",
    "FabricQueryResult",
    "_gen_id",
    "ontology models",
    "Pydantic"
  ],
  "categories": [
    "fabric",
    "ontology",
    "data modeling",
    "Pydantic models"
  ],
  "source_docs": [
    "681eda8faa1f7d09"
  ],
  "backlinks": null,
  "word_count": 375,
  "compiled_at": "2026-04-08T07:32:35Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Fabric Data Models: Ontology Types, Objects, Links, and Queries

## Purpose

These models define the data structures for PocketPaw's ontology layer. They're pure Pydantic models (no database coupling), making them usable across API boundaries, storage layers, and agent reasoning.

## ID Generation

`_gen_id(prefix)` creates IDs like `obj-18f4a2b3c-x7k2` — a prefix, hex millisecond timestamp, and 4-char random suffix. This approach avoids UUID overhead while providing:
- **Sortability**: Timestamp prefix means IDs sort chronologically
- **Debuggability**: The prefix (`ot-`, `obj-`, `lnk-`) tells you the entity type at a glance
- **Collision resistance**: Millisecond precision + random suffix is sufficient for single-node SQLite

## Models

### PropertyDef

Defines a property on an ObjectType. Supports types: `string`, `number`, `boolean`, `date`, `enum`. The `enum_values` field is only relevant when `type == "enum"`. This gives agents schema awareness — they know what properties an ObjectType expects and can validate data before insertion.

### ObjectType

A category definition like "Customer" or "Order". Contains:
- Visual metadata (`icon`, `color`) for dashboard rendering
- `properties` list defining the expected schema
- Timestamps for tracking when types were created/modified

### FabricObject

An instance of an ObjectType. Key fields:
- `type_id` + `type_name`: Links back to the ObjectType (name is denormalized for display convenience)
- `properties`: Arbitrary key-value data matching the type's PropertyDef schema
- `source_connector` + `source_id`: Tracks where the data came from (e.g., a Shopify connector), enabling deduplication and back-references

### FabricLink

A directional relationship between two objects. `link_type` is a freeform string like `"has_orders"` or `"belongs_to"`, giving agents semantic meaning. Links can carry their own properties (e.g., a "purchased" link might have a `quantity` property).

### FabricQuery

Query parameters supporting:
- Filter by type (name or ID)
- Filter by link relationship (`linked_to` + `link_type`)
- Pagination (`limit` + `offset`)
- Arbitrary property filters via `filters` dict

### FabricQueryResult

Wraps query results with a `total` count for pagination and optionally includes related `links`.

## Known Gaps

- `_gen_id` uses `random.choices` which is not cryptographically secure — fine for IDs but shouldn't be used for tokens.
- `datetime.now` in Field defaults uses local time, not UTC. This could cause inconsistencies in distributed deployments.
- No validation that `FabricObject.properties` matches the corresponding `ObjectType.properties` schema — validation would need to happen at the store level.
