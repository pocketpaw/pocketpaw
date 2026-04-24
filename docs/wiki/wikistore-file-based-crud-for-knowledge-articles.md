---
{
  "title": "WikiStore: File-Based CRUD for Knowledge Articles",
  "summary": "WikiStore is the persistence layer for PocketPaw's knowledge subsystem, managing file-based CRUD for raw documents and compiled wiki articles organized by scope. It maintains a predictable three-directory layout per scope and handles index serialization to ensure fast listing without scanning the filesystem on every read.",
  "concepts": [
    "WikiStore",
    "KnowledgeIndex",
    "RawDoc",
    "WikiArticle",
    "file-based storage",
    "scope isolation",
    "path sanitization",
    "YAML frontmatter",
    "idempotency",
    "knowledge pipeline"
  ],
  "categories": [
    "knowledge management",
    "storage",
    "file I/O",
    "persistence"
  ],
  "source_docs": [
    "9010331fa116ceca"
  ],
  "backlinks": null,
  "word_count": 601,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`WikiStore` sits at the heart of PocketPaw's knowledge pipeline. Every scope — a logical namespace like `pocketpaw` or a user-defined tag — gets its own isolated directory tree under `~/.pocketpaw/knowledge/{scope}/`. The store owns three paths within that tree:

- `raw/` — JSON files for `RawDoc` objects, the unprocessed source material ingested from codebases or documents.
- `wiki/` — Markdown files with YAML frontmatter, each representing a compiled `WikiArticle`.
- `index.json` — A `KnowledgeIndex` snapshot that supports fast lookups without iterating the filesystem.

## Why This Structure Exists

The separation of `raw/` and `wiki/` is deliberate. Raw documents are intermediate artifacts — they are ingested, then compiled into articles by an LLM pipeline. Keeping them separate means the store can re-run compilation on existing raw docs without re-ingesting sources, and can delete stale raw docs independently of the compiled output.

The `index.json` file exists because directory listing plus YAML frontmatter parsing would become expensive at scale. By maintaining a denormalized index alongside the articles, callers can fetch article metadata in O(1) without touching individual files.

## Scope Sanitization

The `_sanitize(scope)` function converts an arbitrary scope string into a safe directory name. Without this guard, a scope like `../../etc` would escape the knowledge root and potentially overwrite system files — a classic path traversal risk. The sanitizer strips or replaces any characters that would be problematic on the target filesystem.

## Directory Initialization Guard

`_ensure_dirs()` is called unconditionally in `__init__` using `mkdir(parents=True, exist_ok=True)`. This is an idempotency guard: the constructor works correctly whether the scope directory is being created for the first time or already exists. Without `exist_ok=True`, a second instantiation of `WikiStore` for the same scope would raise `FileExistsError`, making the class unusable as a singleton or in multi-call flows.

## Raw Document Lifecycle

`save_raw(doc)` persists a `RawDoc` as JSON and returns its `Path`. `list_raw()` iterates the `raw/` directory and returns a list of deserialized dicts, allowing the compilation pipeline to discover which raw docs are pending. `delete_raw(doc_id)` enables cleanup after a raw doc has been successfully compiled into a wiki article, keeping the directory lean.

## Article Lifecycle

`save_article(article)` writes a `WikiArticle` to `wiki/` using YAML frontmatter format so the files are human-readable and can be version-controlled. `list_articles()` reads all `.md` files and deserializes them back into `WikiArticle` models. `delete_article(article_id)` allows pruning stale or replaced articles.

## Index Management

`load_index()` reads `index.json` and deserializes it into a `KnowledgeIndex`. If the file does not exist (e.g., on first use), it returns an empty index rather than raising. `save_index(index)` writes the full index atomically. The pattern matches how many file-based databases work: full rewrite on every save keeps consistency simple at the cost of write amplification, which is acceptable given typical knowledge base sizes.

## Operational Utilities

`stats()` returns aggregate counts (raw docs, articles, index entries) without deserializing every file. `clear()` deletes all content under the scope root, used in tests and during full re-ingestion workflows.

## Usage Pattern

```python
from pocketpaw.knowledge.store import WikiStore

store = WikiStore(scope="pocketpaw")

# Persist a raw document
path = store.save_raw(doc)

# List pending raw docs
pending = store.list_raw()

# After compilation, save the article
store.save_article(article)

# Update the index
index = store.load_index()
index.add(article)
store.save_index(index)
```

## Known Gaps

- **No atomic writes**: `save_article` and `save_index` write directly to the target path. A crash mid-write could produce a truncated file. A write-to-temp-then-rename pattern would prevent this.
- **No locking**: concurrent writers to the same scope can corrupt `index.json`. There is no file lock or optimistic concurrency check.
- **`list_raw()` returns dicts, not `RawDoc` models**: callers must deserialize manually, which is inconsistent with `list_articles()` returning typed `WikiArticle` objects.