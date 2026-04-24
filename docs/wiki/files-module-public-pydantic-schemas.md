---
{
  "title": "Files Module Public Pydantic Schemas",
  "summary": "Defines the canonical Pydantic data models shared across the entire files subsystem: `Permission`, `RequestContext`, `FileEntry`, `FolderNode`, `MountConfig`, `ResolvedMount`, `Page`, and `SearchQuery`. Validators on these models enforce invariants (absolute paths, namespaced IDs) that providers cannot violate, ensuring the API response shape is consistent regardless of which provider produced it.",
  "concepts": [
    "Permission",
    "RequestContext",
    "FileEntry",
    "FolderNode",
    "MountConfig",
    "ResolvedMount",
    "Page",
    "SearchQuery",
    "field_validator",
    "model_validator",
    "namespaced IDs",
    "Pydantic",
    "absolute paths",
    "generic types"
  ],
  "categories": [
    "files",
    "schemas",
    "cloud",
    "api"
  ],
  "source_docs": [
    "9e79e013be942529"
  ],
  "backlinks": null,
  "word_count": 492,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.files.schemas` is the contract layer of the files subsystem. Every model defined here is shared across providers, the registry, the service, the router, and the event bus. Changes to these schemas propagate everywhere, so they are designed defensively with validators that catch violations at the point of construction rather than silently passing malformed data downstream.

## Permission

```python
class Permission(BaseModel):
    read: bool = False
    write: bool = False
    manage: bool = False

    def __and__(self, other: Permission) -> Permission: ...
```

Permissions default to `False` -- the least-privilege baseline. The `__and__` operator combines two `Permission` objects by ANDing each field, which is how the RBAC+ABAC two-layer system merges provider-declared permissions with policy evaluator results. Using operator overloading makes the combination site read clearly: `rbac_perm & abac_perm`.

## RequestContext

Carries the authenticated user's identity and workspace context for the duration of a single request. Providers receive this rather than a raw FastAPI `Request` so they remain testable without a running HTTP server. Fields include `user_id`, `workspace_id`, `roles`, and any custom attributes used by ABAC rules.

## FileEntry

The core DTO. Every file and folder in the system is represented as a `FileEntry` regardless of origin. Key fields include `id` (namespaced -- see validator below), `provider_id`, `mount_path`, `name`, `mime`, `size`, `capabilities`, and `permissions`.

### _validate_id_namespace
A `model_validator` that enforces `id` format: IDs must be namespaced with a provider prefix (e.g., `uploads:abc123`, `kb:doc456`). This prevents ID collisions between providers that might otherwise produce the same raw ID from their backing stores. Without this validator, a MongoDB ObjectId from the uploads store could collide with a Go KB document ID.

### _mount_path_absolute
A `field_validator` that ensures `mount_path` always starts with `/`. This prevents paths like `my-files/docs/report.pdf` (relative) from being stored in entries, which would cause longest-prefix matching in the registry to fail silently.

## FolderNode

Represents a node in the file tree returned by `GET /files/tree`. Contains `path`, `name`, `children` (list of `FolderNode`), and `mounts` (list of `ResolvedMount` that anchor at this node). The `_path_absolute` validator mirrors the one on `FileEntry.mount_path`.

## MountConfig and ResolvedMount

`MountConfig` is the static mount definition loaded from `mounts.yaml`. `ResolvedMount` is produced at request time by expanding `MountConfig`'s path template with user/workspace variables. Keeping them separate means the static config can be cached indefinitely while resolved mounts are ephemeral.

## Page[T]

A generic paginated response wrapper. Carries `items: list[T]`, `next_cursor: str | None`, and `total: int | None`. Providers return `Page[FileEntry]`; the router returns `Page[FileEntry]` to clients. Using a generic avoids duplicating the pagination envelope for different entry types.

## SearchQuery

Carries `q` (query string), `workspace_id`, `limit`, and optional `provider_id` filter. Passed to provider `search` methods.

## Known Gaps

- **No `updated_at` on `FolderNode`.** Folder nodes in the tree have no modification timestamp, making it impossible to determine whether a cached tree snapshot is stale.
- **No schema versioning.** Adding a required field to `FileEntry` would break all providers that construct entries without the new field, with no migration path.