---
{
  "title": "Cloud Document Models Registry — Beanie Initialization Manifest",
  "summary": "The central re-export module for all Beanie ODM documents in the enterprise cloud layer, providing a stable import surface and a lazy-loading `ALL_DOCUMENTS` list consumed by Beanie's init sequence. Special handling defers `FileUpload` and `FileFolder` imports to break circular import chains with the uploads subsystem.",
  "concepts": [
    "Beanie ODM",
    "document registry",
    "ALL_DOCUMENTS",
    "lazy loading",
    "circular imports",
    "FileUpload",
    "FileFolder",
    "init_beanie",
    "TimestampedDocument",
    "cloud models",
    "Pydantic re-export"
  ],
  "categories": [
    "data modeling",
    "MongoDB",
    "architecture",
    "Beanie"
  ],
  "source_docs": [
    "f63ad87fae2d3a33"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/models/__init__.py` serves two purposes: it re-exports every Beanie document class for convenient importing, and it provides `get_all_documents()` / `ALL_DOCUMENTS` for use in `init_beanie()`. When PocketPaw initializes its MongoDB connection, it passes the document list to Beanie so the ODM can register collection mappings and ensure indexes exist.

## The Lazy ALL_DOCUMENTS List

Beanie's `init_beanie()` function accepts a list of document classes. A naive implementation would be:

```python
ALL_DOCUMENTS = [User, Agent, Pocket, ...]
```

But this fails when `FileUpload` and `FileFolder` are in the list — those classes live in `ee.cloud.uploads.models`, which has a circular import dependency with the main models package. The solution is `_LazyAllDocuments`, a `list` subclass that defers its population until first access:

```python
class _LazyAllDocuments(list):
    def _ensure_loaded(self):
        if not self._loaded:
            docs = get_all_documents()  # triggers _ensure_file_upload()
            self.extend(docs)
            self._loaded = True
```

By overriding `__iter__`, `__len__`, `__getitem__`, and `__contains__`, `_LazyAllDocuments` behaves exactly like a list but delays the import of uploads models until the list is first accessed — which is at Beanie init time, after all modules are fully loaded and the circular dependency is resolved.

## FileUpload Lazy Import

`_ensure_file_upload()` defers the import of `FileUpload` and `FileFolder` specifically:

```python
FileUpload: type = None
FileFolder: type = None

def _ensure_file_upload():
    global FileUpload, FileFolder
    if FileUpload is None:
        from ee.cloud.uploads.models import FileUpload as _FileUpload
        from ee.cloud.uploads.models import FileFolder as _FileFolder
        FileUpload = _FileUpload
        FileFolder = _FileFolder
    return FileUpload
```

The module-level `FileUpload = None` placeholder means `from ee.cloud.models import FileUpload` works at import time — callers get `None` if they import before init, but that is caught at runtime rather than crashing at import time. The `# type: ignore[assignment]` suppresses the type checker's objection to assigning `None` to a `type` variable.

## Document Registry

The 14 documents registered with Beanie cover the full cloud schema: `User`, `Agent`, `Pocket`, `Session`, `Comment`, `Notification`, `FileObj`, `FileUpload`, `FileFolder`, `Workspace`, `Invite`, `Group`, `Message`, and `ReadState`. Any new Beanie document must be added here and to `get_all_documents()` to have its collection and indexes created on startup.

## Import Surface

The `__all__` list exports 30+ symbols, including both document classes and embedded Pydantic models (e.g., `AgentConfig`, `CommentAuthor`, `WorkspaceMembership`). This allows callers to write `from ee.cloud.models import Agent, AgentConfig` rather than navigating to the specific submodule.

## Known Gaps

- `FileUpload` and `FileFolder` appear in `__all__` but are `None` at module import time. Code that imports them from `ee.cloud.models` without calling `get_all_documents()` first will get `None` rather than a class, which will fail at instantiation time with a confusing error.
- `MemoryFactDoc` is not in this registry — it is registered separately by the memory bootstrap path, which means the document's indexes are created on a different code path than all other documents.