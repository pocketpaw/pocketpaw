---
{
  "title": "KbProvider: Knowledge Base File Provider",
  "summary": "Exposes workspace Knowledge Base documents as a browseable file mount in the unified files tree. `KbProvider` delegates reads to a `_KbService` Protocol adapter rather than directly calling the KB FastAPI routes, decoupling the files subsystem from the KB module's current route-and-Go-binary architecture.",
  "concepts": [
    "KbProvider",
    "_KbService",
    "FolderProvider",
    "Protocol adapter",
    "Knowledge Base",
    "workspace mounts",
    "baseline_rbac",
    "_to_entry",
    "Go binary adapter",
    "bootstrap Task 14",
    "FileEntry",
    "access control"
  ],
  "categories": [
    "files",
    "providers",
    "knowledge-base",
    "cloud"
  ],
  "source_docs": [
    "f07a97a562e2dd7a"
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

`KbProvider` bridges two subsystems that were built independently: the cloud files tree (which expects a `FolderProvider` interface) and the Knowledge Base module (which currently exposes its data only through FastAPI routes that shell out to the `kb` Go binary). The provider makes KB documents appear as standard `FileEntry` objects within the files tree, without duplicating the KB module's access checks.

## The _KbService Protocol

Rather than importing and calling KB module internals directly, `KbProvider.__init__` accepts a `_KbService` Protocol object:

```python
class _KbService(Protocol):
    async def list_documents(self, workspace_id: str, *, limit: int) -> list[dict]: ...
    async def get_document(self, doc_id: str, *, workspace_id: str) -> dict: ...
```

This indirection is deliberate. The KB module today has no async service object -- operations go through `_kb("list", ...)` shell calls in FastAPI route handlers. A thin adapter must be constructed at bootstrap time (Task 14) that wraps those shell calls behind the Protocol interface. By declaring the Protocol here, `KbProvider` tests can use a mock without any dependency on the Go binary, and the real adapter can be swapped in when it exists.

## Mount Structure

`list_mounts` returns one `ResolvedMount` per workspace the requesting user belongs to. This means each workspace gets its own KB folder in the files tree (e.g., `/workspaces/abc123/knowledge-base`). Non-members of a workspace see no mount at all because `list_documents` returns empty for workspaces the user is not entitled to -- the KB module's existing entitlement logic is reused rather than duplicated.

## Access Control

`baseline_rbac` maps workspace roles to `Permission` objects:
- Workspace members -> `read` only
- Workspace admins and owners -> `read + write + manage`

Because `KbProvider` does not surface upload, rename, move, or delete methods (inheriting `ProviderUnsupported` stubs from `BaseFolderProvider`), write and manage permissions exist on the `Permission` object but cannot be exercised through the files tree today. Write access to KB documents goes through the dedicated KB endpoints, not the generic files tree.

## _to_entry Mapping

The `_to_entry` method normalises KB document dicts into `FileEntry` objects. It maps `id`, `title` (as filename), `mime`, `size`, `owner_id`, `workspace_id`, and timestamps. Tags are preserved in `FileEntry.tags` for ABAC rule evaluation downstream.

## Known Gaps

- **No `_KbService` implementation exists yet.** `bootstrap.py` (Task 14) must create the adapter that wraps `_kb("list", ...)` calls. Without it, `KbProvider` cannot be registered.
- **No pagination support.** `list_entries` fetches up to a hardcoded `limit` from the service but does not support cursor-based pagination. KB workspaces with large document counts will be truncated.
- **Read-only.** Upload, rename, move, and delete all raise `ProviderUnsupported`. KB document management must go through KB-specific endpoints.