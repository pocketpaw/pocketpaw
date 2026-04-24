---
{
  "title": "Workspace-Scoped Upload URL Resolver with Cross-Tenant Isolation",
  "summary": "EEUploadResolver translates opaque upload URLs into local filesystem paths while enforcing workspace isolation — a lookup for a file that exists in a different workspace returns `None`, preventing existence leaks across tenants. It is used by agents and media processors that need to access uploaded file content.",
  "concepts": [
    "EEUploadResolver",
    "workspace isolation",
    "cross-tenant isolation",
    "upload URL",
    "parse_upload_url",
    "StorageAdapter",
    "MongoFileStore",
    "local_path",
    "existence oracle",
    "resolve_media_paths_scoped"
  ],
  "categories": [
    "uploads",
    "security",
    "cloud EE",
    "file management"
  ],
  "source_docs": [
    "267e6847cc44a7ca"
  ],
  "backlinks": null,
  "word_count": 428,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/resolver.py` provides `EEUploadResolver`, which wraps the OSS `UploadResolver` semantics with EE-specific workspace isolation. Any code that needs to convert an upload URL (e.g., `pocketpaw-upload://file-abc123`) into a local path that the process can read should go through this resolver.

## Why Workspace Isolation at the Resolver Level

Without workspace checks, an agent or endpoint that receives a URL for a file uploaded in workspace A could resolve it to a local path and read that file — even if the current request is operating in workspace B. The cross-tenant data leak would be invisible at the HTTP layer (no 403, no audit log) because the file exists on disk and is readable by the process.

`EEUploadResolver.resolve` performs a `MongoFileStore.get_scoped(file_id, workspace=workspace)` lookup. If the file exists but belongs to a different workspace, `get_scoped` returns `None` and the resolver returns `None` — treated identically to a nonexistent file. This prevents existence oracle attacks: a caller in workspace B cannot determine whether a given file ID exists in workspace A by probing the resolver.

## Adapter Failure Containment

Even after a successful metadata lookup, the call to `adapter.local_path(storage_key)` is wrapped in a `try/except`:

```python
try:
    return self._adapter.local_path(rec.storage_key)
except Exception:
    logger.exception("upload adapter.local_path failed ...")
    return None
```

This prevents unexpected adapter failures — permission errors on the storage root, remount races, or future remote adapter exceptions — from propagating into chat or agent response flows as unhandled exceptions. The caller gets `None` and logs the failure, rather than a 500 error that could abort message delivery.

## resolve_media_paths_scoped

A companion async function processes a list of URL strings, resolving upload URLs to local paths and passing through non-upload strings unchanged. Unresolvable upload URLs are silently dropped. This mirrors the OSS `resolve_media_paths` behavior, making the EE version a drop-in for callers that already use the OSS function but need workspace isolation.

## default_resolver

`default_resolver()` returns an `EEUploadResolver` pre-wired to the module-level adapter and `MongoFileStore` singletons. This avoids callers needing to construct the resolver themselves and ensures consistent adapter configuration across all call sites.

## Known Gaps

- The resolver only handles `pocketpaw-upload://` scheme URLs. External URLs (e.g., signed S3 URLs, CDN links) pass through `parse_upload_url` as `None` and are returned unchanged by `resolve_media_paths_scoped`. There is no resolver path for remote storage keys in this implementation.
- `local_path` on the adapter returns a `Path` object pointing to a local file. If the storage adapter is swapped to a remote backend (S3, GCS), `local_path` would not be meaningful. The resolver API would need to return a stream or signed URL instead of a `Path`.