---
{
  "title": "BaseFolderProvider: Default Provider Stub Implementation",
  "summary": "Defines `BaseFolderProvider`, the default base class for all file providers, where every operation raises `ProviderUnsupported` unless overridden. This design enforces capability-based provider contracts: concrete providers only implement the operations they actually support, and the error type communicates unsupported operations cleanly to the API layer.",
  "concepts": [
    "BaseFolderProvider",
    "ProviderUnsupported",
    "FolderProvider protocol",
    "list_mounts",
    "list_entries",
    "open_stream",
    "upload",
    "baseline_rbac",
    "async iterator",
    "capability-based design",
    "Page",
    "FileEntry"
  ],
  "categories": [
    "files",
    "providers",
    "cloud",
    "architecture"
  ],
  "source_docs": [
    "34a0cc7143050da7"
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`BaseFolderProvider` is the default base class for all `FolderProvider` implementations in PocketPaw's cloud files subsystem. Every method raises `ProviderUnsupported` by default. Concrete providers override only the operations their backing store supports. This is sometimes called the "stub-then-override" pattern, and it serves several purposes specific to a multi-provider file system.

## Why Raise Rather Than Return Empty?

A naive base class might return empty lists or `None` for unsupported operations. The problem with that approach is that the router and service layers cannot distinguish "operation succeeded but returned no results" from "this provider does not support this operation." Raising `ProviderUnsupported` produces a 405 Method Not Allowed response, which the frontend can use to disable UI controls (the rename button, the delete button) on files that come from read-only providers.

## Method Inventory

### list_mounts(ctx) -> list[ResolvedMount]
Returns the mounts this provider exposes to the current user context. Providers that surface dynamic mounts (e.g., one mount per workspace) override this. Providers with a single static mount can return a hardcoded list.

### list_entries(ctx, mount_path, cursor, limit, filters) -> Page[FileEntry]
Pages through the files and folders at a given mount path. The `cursor` supports keyset pagination -- providers that cannot paginate efficiently (e.g., those backed by flat stores) must still respect `limit` to avoid unbounded responses.

### get_entry(ctx, entry_id) -> FileEntry
Fetches a single entry by ID. Used by the router for single-file detail views and by the permission evaluator when it needs to check an entry the listing did not return.

### open_stream(ctx, entry_id) -> AsyncIterator[bytes]
Returns an async byte stream for download. Returning an async iterator rather than bytes allows streaming large files without buffering them in memory. Providers that do not support downloads override this to raise `ProviderUnsupported`.

### upload(ctx, mount_path, upload) -> FileEntry
Handles file uploads. The `upload` argument carries the stream and metadata. Providers return the created `FileEntry` so the router can return it to the client immediately without a round-trip `get_entry` call.

### rename, move, delete
Mutation operations. `move` is constrained to within-scope moves; cross-scope moves raise `CrossScopeMove` at the service level before reaching the provider.

### search(ctx, query) -> Page[FileEntry]
Full-text or metadata search across a provider's files. Providers that cannot search raise `ProviderUnsupported`; the aggregator skips them silently.

### baseline_rbac(ctx, entry) -> Permission
The only non-async method. Returns the RBAC `Permission` for a given entry based on the authenticated user's relationship to it (owner, admin, member). This is synchronous because RBAC decisions are expected to be computed from already-loaded context, not from additional I/O.

## Integration with the Error Hierarchy

`BaseFolderProvider` imports only `ProviderUnsupported` from the errors module -- not the full hierarchy. Concrete providers import additional error types as needed. This keeps the base class dependency surface minimal.

## Known Gaps

- **No `copy_entry` method.** Cross-scope moves require a copy-then-delete, but `BaseFolderProvider` has no `copy_entry` stub. When cross-scope moves are implemented, this gap will surface.
- **`open_stream` has no timeout or size limit.** Streaming large files from slow upstream providers (Google Drive, SharePoint) could block event loop threads indefinitely.