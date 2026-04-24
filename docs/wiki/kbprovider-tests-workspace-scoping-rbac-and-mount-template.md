---
{
  "title": "KbProvider Tests: Workspace Scoping, RBAC, and Mount Template",
  "summary": "This module tests the KbProvider file provider, which surfaces the knowledge base as a virtual file tree. Tests verify that entries are scoped to the correct workspace, that baseline RBAC rules grant read access to workspace members and manage access to admins, and that the mount path template correctly resolves the workspace ID.",
  "concepts": [
    "KbProvider",
    "knowledge base",
    "virtual file tree",
    "ProviderContract",
    "workspace scoping",
    "baseline RBAC",
    "Permission",
    "mount template",
    "kb: prefix",
    "FakeKbService",
    "list_entries",
    "list_mounts"
  ],
  "categories": [
    "testing",
    "files",
    "knowledge base",
    "RBAC",
    "test"
  ],
  "source_docs": [
    "tests/cloud/files/providers/test_kb_provider.py"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_kb_provider.py` covers `KbProvider`, the file provider that maps knowledge base documents into the unified virtual file tree. It uses both a `_FakeKbService` stub (for unit-level tests) and the shared `ProviderContract` base class (for protocol compliance tests).

## _FakeKbService

```python
class _FakeKbService:
    def __init__(self, docs):
        self._docs = docs

    async def list_documents(self, workspace_id, *, limit=500):
        return list(self._docs)

    async def get_document(self, doc_id, *, workspace_id):
        for d in self._docs:
            if d["id"] == doc_id:
                return d
        raise KeyError(doc_id)
```

`_FakeKbService` provides the two KB service methods `KbProvider` calls, returning pre-configured document dicts. This lets tests control exactly which documents exist without a real knowledge base service or database.

## Provider Contract Compliance

`TestKbProviderContract` extends `ProviderContract` and implements `build_provider()`:

```python
class TestKbProviderContract(ProviderContract):
    def build_provider(self):
        return KbProvider(service=_FakeKbService([_doc()]))
```

`ProviderContract` is a shared base class that runs a standard set of protocol compliance tests against any provider. By extending it, `KbProvider` must pass the same contract tests as every other provider (e.g., `UploadsProvider`). This ensures `KbProvider` can be used interchangeably in the provider registry.

## Workspace Scoping

`test_kb_list_entries_scoped_to_workspace` creates two documents (`id="a"`, `id="b"`) and calls `list_entries` with the workspace's mount path. The assertion checks that both entries are returned with their IDs prefixed as `kb:a` and `kb:b`. The `kb:` prefix is the provider's namespace in the virtual file system, distinguishing KB entries from uploads and other providers.

The workspace scoping test also implicitly verifies that the mount path includes the workspace ID (`/Workspaces/ws_1/Knowledge Base`), ensuring that a KbProvider for workspace `ws_1` does not serve documents from workspace `ws_2`.

## Baseline RBAC

Two RBAC tests cover the access control rules for KB entries:

### Workspace Member Gets Read
`test_kb_baseline_rbac_workspace_member_reads` asserts that a `RequestContext` with `role="member"` returns `Permission.READ` from `baseline_rbac`. Knowledge base documents are workspace-readable — any workspace member should be able to see them.

### Admin Gets Manage
`test_kb_baseline_rbac_admin_manages` asserts that a `RequestContext` with `role="admin"` returns `Permission.MANAGE`. Admins can add, edit, and delete KB documents; members can only read them.

These tests pin the RBAC decision at the provider level. If the permission model changes (e.g., introducing a writer role), these tests will fail and require explicit updates.

## Mount Template Resolution

`test_kb_mount_template_resolves_workspace_id` verifies that the provider correctly resolves its mount path template by substituting the workspace ID from the `RequestContext`. The resolved mount must include the literal workspace ID string rather than a template placeholder. This is tested because a template resolution bug would cause mount paths to mismatch between `list_mounts` and `list_entries` calls.

## Known Gaps

No TODOs or FIXMEs are present. The `_FakeKbService.list_documents` does not respect the `limit` parameter — all documents are always returned. Tests that need to verify limit enforcement would need to add that behavior to the stub.
