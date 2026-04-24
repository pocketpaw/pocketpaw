---
{
  "title": "Cloud Files Schema Validation Tests: FileEntry, MountConfig, Permission, and Page",
  "summary": "This module validates the Pydantic schema constraints for PocketPaw's core cloud files data models, confirming invariants like namespaced entry IDs, enumerated scopes, valid capability sets, absolute mount templates, and permission intersection semantics. These tests prevent malformed data from entering the system by verifying that validation errors are raised at construction time.",
  "concepts": [
    "Pydantic validation",
    "FileEntry",
    "MountConfig",
    "Permission",
    "FolderNode",
    "Page",
    "RequestContext",
    "namespaced IDs",
    "capability validation",
    "schema constraints"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Data Models",
    "Schema Validation",
    "test"
  ],
  "source_docs": [
    "9bb8db6f9e4c9d68"
  ],
  "backlinks": null,
  "word_count": 508,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_schemas.py` is the schema contract test suite for `ee.cloud.files.schemas`. It validates that Pydantic's model validators enforce the business rules embedded in the data models. Each test exercises one specific constraint, making it easy to identify which rule a failing assertion corresponds to.

## Core Models Under Test

- **`FileEntry`**: represents a single file in the virtual filesystem, with ID, provider affiliation, scope, capabilities, and tags.
- **`FolderNode`**: a recursive tree node for the folder hierarchy.
- **`MountConfig`**: a mount template declaration (provider ID, path template, writability, order).
- **`Permission`**: a three-bit RBAC permission triplet supporting bitwise AND (`&`).
- **`Page[T]`**: a generic paginated response container.
- **`RequestContext`**: the authenticated caller's identity and attributes.

## Test Breakdown

### `test_file_entry_id_must_be_namespaced`

`FileEntry.id` must contain a colon, e.g., `"uploads:abc"`. A plain string like `"no-colon"` raises `ValidationError`. This enforces the global uniqueness invariant: IDs from different providers are distinguished by their namespace prefix.

### `test_file_entry_id_prefix_matches_provider`

The prefix of `id` before the colon must match `provider_id`. An entry with `id="kb:abc"` and `provider_id="uploads"` raises `ValidationError`. Without this, an entry could be routed to the wrong provider when looked up by ID.

```python
with pytest.raises(ValidationError):
    _entry(id="kb:abc", provider_id="uploads")
```

### `test_file_entry_scope_is_enum`

The `scope` field accepts only valid enumeration values (e.g., `"personal"`, `"workspace"`). An arbitrary string like `"nope"` raises `ValidationError`, preventing free-form scope strings from polluting query filters and access control logic.

### `test_file_entry_capabilities_subset`

The `capabilities` list must contain only known operation names. An unrecognized capability like `"teleport"` raises `ValidationError`. This prevents providers from advertising fantasy capabilities that the UI or permission layer would not know how to handle.

### `test_folder_node_children_are_folder_nodes`

Constructs a two-level `FolderNode` hierarchy and confirms the child node retains its `provider_id`. This verifies that recursive Pydantic models serialize and reconstruct correctly, which is critical for deep folder trees.

### `test_mount_config_rejects_non_absolute`

A `mount_template` that does not start with `/` raises `ValidationError`. This prevents relative mount paths from entering the system, which would make path resolution ambiguous.

```python
with pytest.raises(ValidationError):
    MountConfig(provider_id="uploads", mount_template="My Files", ...)
```

### `test_permission_merge_is_intersection`

`Permission` supports the `&` operator, which returns the boolean AND of each field:

```python
a = Permission(read=True, write=True, manage=False)
b = Permission(read=True, write=False, manage=False)
assert (a & b) == Permission(read=True, write=False, manage=False)
```

This is the mechanism that combines RBAC permissions from multiple sources without ever escalating above what any single source allows. The `&` semantics ensure the result is always the most restrictive intersection.

### `test_page_carries_cursor_and_items`

Confirms that `Page[int]` correctly stores `items` and `next_cursor`. The generic type parameter is verified at runtime through Pydantic, ensuring the paginated response structure is consistent across all providers.

### `test_request_context_requires_user_id`

Constructing `RequestContext` without a `user_id` raises `ValidationError`. This prevents unauthenticated contexts from entering the permission evaluation pipeline -- any code path that constructs a `RequestContext` must supply a user identity.

## Known Gaps

There are no tests for `FileEntry` with very long tag lists or tags that contain special characters, which may interact unexpectedly with ABAC rule matching. Additionally, the `source_ref` field is not validated beyond being a dict, so malformed source references would not be caught at schema validation time.
