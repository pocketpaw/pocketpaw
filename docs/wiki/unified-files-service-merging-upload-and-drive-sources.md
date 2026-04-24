---
{
  "title": "Unified Files Service: Merging Upload and Drive Sources",
  "summary": "Implements `UnifiedFilesService`, the Cluster E sub-PR 4 facade that merges chat S3 uploads and (stub) Google Drive files into a single flat list for the legacy `GET /files` endpoint. A `_dedupe` function drops duplicates by content identity to prevent the same file appearing twice when sources overlap.",
  "concepts": [
    "UnifiedFilesService",
    "UnifiedFile",
    "list_chat_uploads",
    "list_drive",
    "_dedupe",
    "MongoFileStore",
    "Google Drive stub",
    "Cluster E",
    "FilesPanel",
    "content-identity dedup",
    "GET /files",
    "legacy service"
  ],
  "categories": [
    "files",
    "service",
    "cloud",
    "uploads"
  ],
  "source_docs": [
    "f43af09fae7b2f0a"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.files.service` is the legacy facade backing the flat `GET /files` endpoint. Its job is to pull files from multiple independent sources, merge them into a uniform `UnifiedFile` list, and deduplicate the result. This module predates the `ProviderRegistry` architecture and represents the Cluster E sub-PR 4 integration point.

## UnifiedFile

`UnifiedFile` is a dataclass (not a Pydantic model) that defines the row shape the `FilesPanel` frontend renders. Fields include `filename`, `size`, `mime`, `source` (which origin produced it), `url`, and `created_at`. Using a dataclass rather than Pydantic here is a deliberate choice: the service is an internal aggregator that doesn't need input validation, only output consistency.

## UnifiedFilesService

`UnifiedFilesService` is a stateless facade initialised with an `uploads` MongoFileStore reference. It exposes two async listing methods:

### list_chat_uploads(workspace_id, *, limit) -> list[UnifiedFile]
Queries `MongoFileStore` for files uploaded through the chat interface in the given workspace. These are files users attached to chat messages -- they live in S3 but are indexed in MongoDB. The `limit` parameter prevents unbounded queries; the default matches the `FilesPanel`'s page size.

### list_drive(workspace_id, *, limit) -> list[UnifiedFile]
Currently returns an empty list. Google Drive integration is planned under Cluster C, which owns the connector-status endpoint. The stub exists so the `GET /files` router endpoint can fan out to Drive without changing its response shape when the real implementation arrives. The module docstring documents the exact handshake: once Cluster C's connector-status endpoint lands, this method can call it to check which workspaces have a connected Drive account before attempting a Drive listing.

## _dedupe

```python
def _dedupe(files: list[UnifiedFile]) -> list[UnifiedFile]:
    # Drop later duplicates keyed on (filename, size, mime).
```

When sources overlap -- for example, a file that was uploaded via chat and also synced from Drive -- the same file could appear twice in the merged list. `_dedupe` prevents this by keying on `(filename, size, mime)` and keeping only the first occurrence. This is a content-identity heuristic, not a true dedup by ID, which means two genuinely different files with the same name, size, and mime type would be collapsed to one. This is a known trade-off accepted for Phase 1.

## Relationship to ProviderRegistry

`UnifiedFilesService` does not use `ProviderRegistry`. It is a pre-registry service that will eventually be superseded by the tree/browse API. The flat `GET /files` endpoint is preserved for backward compatibility with the existing `FilesPanel` frontend. New navigation features use the registry-backed tree endpoints.

## Known Gaps

- **Drive is stubbed.** `list_drive` always returns `[]`. No Drive data appears in `GET /files` today.
- **Content-identity dedup can false-positive.** Two files with identical `(filename, size, mime)` but different content will be collapsed.
- **No sorting.** The merged list order depends on which source is queried first. There is no `created_at`-based sort across sources.