---
{
  "title": "Knowledge Base Domain Package Initialiser",
  "summary": "The package initialiser for the `ee.cloud.kb` Knowledge Base domain, which exposes workspace-scoped KB endpoints including search, ingest, browse, lint, and stats. The comment header documents the package's scope and public surface, serving as the entry point for the KB subsystem.",
  "concepts": [
    "ee.cloud.kb",
    "Knowledge Base",
    "workspace-scoped",
    "search",
    "ingest",
    "browse",
    "lint",
    "stats",
    "Go binary",
    "_kb helper",
    "KbProvider",
    "FastAPI routes",
    "backend_adapter"
  ],
  "categories": [
    "knowledge-base",
    "cloud",
    "enterprise",
    "architecture"
  ],
  "source_docs": [
    "1de1fdb55264cdfa"
  ],
  "backlinks": null,
  "word_count": 420,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee/cloud/kb/__init__.py` file is the namespace anchor for PocketPaw's enterprise Knowledge Base subsystem. Like all Python package initialisers, its primary role is structural -- it transforms the `kb/` directory into the importable `ee.cloud.kb` namespace. However, the comment header embedded in this file carries meaningful documentation about the package's intended scope.

## Package Scope

According to the file header:

> Created: Knowledge base domain package for ee/cloud.
> Exposes workspace-scoped KB endpoints (search, ingest, browse, lint, stats).

This declares that `ee.cloud.kb` is the canonical home for all KB-related HTTP endpoints and domain logic within the EE (Enterprise Edition) cloud module. The five named operations map to the KB subsystem's functional surface:

- **search** -- Full-text and semantic search across KB documents within a workspace
- **ingest** -- Processing and indexing new documents into the KB
- **browse** -- Listing and navigating KB documents (also surfaced through `KbProvider` in the files tree)
- **lint** -- Validating KB document quality or structure
- **stats** -- Workspace-level KB usage statistics

## Relationship to the Files Subsystem

The `ee.cloud.kb` package has a bidirectional relationship with `ee.cloud.files`. The `KbProvider` in `ee.cloud.files.providers.kb` surfaces KB documents as browseable entries in the files tree, delegating to a `_KbService` Protocol adapter that wraps KB operations. This means the files tree is a read-only view of KB content; writes and management go through dedicated `ee.cloud.kb` endpoints.

## Architecture: Routes to Go Binary

The KB module currently implements operations by shelling out to the `kb` Go binary via an internal `_kb(...)` helper. This is a deliberate architecture choice: the Go binary provides high-performance BM25 search and indexing that would be slow to reimplement in Python. The FastAPI routes in `ee.cloud.kb` act as thin wrappers that handle authentication, workspace scoping, and request validation before delegating to the binary.

The `backend_adapter.py` module in this package provides the reverse bridge: it exposes PocketPaw's agent LLM backends to the `knowledge_base` Python package's compiler pipeline, so KB article compilation uses whatever AI backend is currently active rather than being hardcoded to a specific model.

## Known Gaps

- **No async service object.** Operations go through route handlers that call `_kb(...)`. This makes it difficult for the `KbProvider` in the files tree to call KB operations directly without going through HTTP. Task 14 (bootstrap) must create a `_KbService` adapter that wraps these shell calls behind a Protocol interface.
- **Lint and stats endpoints are not documented here.** The comment declares them as part of the package scope, but their implementations are not referenced from this file.