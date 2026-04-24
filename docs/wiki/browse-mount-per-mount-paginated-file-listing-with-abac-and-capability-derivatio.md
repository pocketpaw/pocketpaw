---
{
  "title": "Browse Mount - Per-Mount Paginated File Listing with ABAC and Capability Derivation",
  "summary": "The `browse_mount` function handles a single paginated directory listing request against one virtual mount, applying ABAC filtering and deriving fine-grained capabilities for each visible entry. It is the core read path for the Files Tab browser.",
  "concepts": [
    "browse_mount",
    "ABAC filtering",
    "capability derivation",
    "MountNotFound",
    "FolderProvider",
    "cursor pagination",
    "ResolvedMount",
    "baseline_rbac",
    "derive_capabilities",
    "FileEntry",
    "Page"
  ],
  "categories": [
    "files",
    "cloud EE",
    "ABAC",
    "API"
  ],
  "source_docs": [
    "4ec4c5886da31e73"
  ],
  "backlinks": null,
  "word_count": 389,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`browse_mount` is the function that powers a single page of the Files Tab browser. Given a mount path, pagination cursor, and filter parameters, it:

1. Resolves the virtual mount to a concrete provider.
2. Fetches a raw page of entries from the provider.
3. Applies ABAC filtering to remove entries the user cannot see.
4. Derives per-entry capabilities (read, write, delete, etc.) from RBAC + ABAC state.
5. Returns a `Page[FileEntry]` with capabilities attached.

## Mount Resolution

```python
mount = registry.resolve_mount(path=mount_path, variables=variables)
try:
    provider = registry.get(mount.provider_id)
except KeyError as exc:
    raise MountNotFound(mount_path) from exc
```

`resolve_mount` maps the URL path to a `ResolvedMount` (including captured path variables such as workspace ID). `registry.get` then looks up the registered `FolderProvider`. The `KeyError -> MountNotFound` translation is important: a raw `KeyError` bubbling to the HTTP layer would produce a 500; the typed `MountNotFound` maps to a 404 `files.mount_not_found`, which the client can handle gracefully.

## ABAC Filtering

```python
filtered = apply_abac(raw.items, ctx=ctx, rules=rules)
```

`apply_abac` removes entries whose tags trigger ABAC rules that the current user's attributes do not satisfy. Filtered entries are silently omitted - callers cannot distinguish between 'the directory is empty' and 'there are files you cannot see', which is the intended behaviour for confidential entries.

## Capability Derivation

For each surviving entry, the function derives capabilities:

```python
rbac = provider.baseline_rbac(ctx, e)
abac_allowed = rules.allows(tags=e.tags, attributes=ctx.attributes)
caps = derive_capabilities(
    entry=e, rbac=rbac, mount_writable=mount.writable, abac_allowed=abac_allowed
)
```

`provider.baseline_rbac` returns what the user can do based on their role. `derive_capabilities` combines this with the mount's `writable` flag and the ABAC result to produce the final `Capability` set shown to the client. The UI uses these capabilities to decide which action buttons to display.

Computing `abac_allowed` per-entry now (even though it is currently binary) ensures that when the ruleset gains per-capability restrictions, the derivation logic is already in the right shape without requiring a refactor.

## Pagination

`raw.next_cursor` is passed through unchanged from the provider. Providers generate and interpret cursors; `browse_mount` is cursor-agnostic. The `Page` response either contains `next_cursor` (more pages available) or `None` (end of results).

## Known Gaps

- ABAC filtering silently removes entries with no 'N items hidden' count in the response.
- `apply_abac` iterates all entries returned by the provider before filtering. For very large pages with many filtered entries, provider-side ABAC push-down would be more efficient.