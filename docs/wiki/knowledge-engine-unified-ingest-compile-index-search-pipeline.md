---
{
  "title": "Knowledge Engine: Unified Ingest-Compile-Index-Search Pipeline",
  "summary": "The `KnowledgeEngine` class is the single entry point for all knowledge management operations — ingesting text, URLs, and files, compiling them into structured wiki articles via LLM, indexing concepts and backlinks, and searching with BM25. It is scope-aware, allowing separate knowledge bases per agent, workspace, or pocket.",
  "concepts": [
    "KnowledgeEngine",
    "WikiArticle",
    "RawDoc",
    "ingest pipeline",
    "BM25 search",
    "LLM compilation",
    "knowledge scope",
    "concept index",
    "backlinks",
    "recompile",
    "lint"
  ],
  "categories": [
    "knowledge",
    "search",
    "AI pipeline"
  ],
  "source_docs": [
    "55bcbacd868de86b"
  ],
  "backlinks": null,
  "word_count": 355,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/__init__.py` defines `KnowledgeEngine`, the orchestrator for PocketPaw's knowledge subsystem. A knowledge engine instance is scoped to a specific entity — an agent, a workspace, or a pocket — and manages the full lifecycle of knowledge within that scope.

## Scope-Aware Architecture

```python
engine = KnowledgeEngine(scope="agent:abc123")
engine = KnowledgeEngine(scope="workspace:ws1")
engine = KnowledgeEngine(scope="pocket:p1")
```

The scope string partitions knowledge on disk and in search. An agent's knowledge never bleeds into a workspace's knowledge, which is critical for multi-agent deployments where different agents have different domain expertise.

## The Four-Stage Pipeline

```
ingest -> compile -> index -> search
```

**1. Ingest**: Raw content (text, URL, file) is extracted and stored as a `RawDoc` with a content hash ID. Raw docs are preserved so they can be recompiled if the compile prompt improves.

**2. Compile**: The LLM transforms raw text into a `WikiArticle` — a structured document with title, summary, full markdown content, concepts, and categories. This is the step that elevates the system above naive RAG.

**3. Index**: The `KnowledgeIndex` is updated with the new article's concepts and backlinks.

**4. Search**: BM25 over compiled articles returns ranked results.

## Key Methods

```python
# Ingest sources
await engine.ingest_text("Annual revenue was $4.2M", source="report-2025")
await engine.ingest_url("https://company.com/about")
await engine.ingest_file(Path("contract.pdf"))

# Search
results = await engine.search("revenue projections", limit=5)
context = await engine.search_context("Q4 goals", max_chars=4000)

# Browse
articles = engine.list_articles()
concepts = engine.list_concepts()

# Maintenance
issues = await engine.lint()
articles = await engine.recompile_all()
engine.clear()
```

## Why Not RAG?

The design comment in the source is explicit: this is a knowledge engine, not RAG. The compile step has the LLM do semantic work upfront — extracting structure, identifying concepts, writing summaries — so that search over compiled articles is fast and deterministic. The LLM cost is paid once at ingest time, not at every query.

## Known Gaps

- **No incremental update detection**: If a URL is ingested twice, it creates a new RawDoc if the page content has changed. There is no deduplication or change-detection layer.
- **No cross-scope search**: Each engine is scoped to a single entity. There is no built-in way to search across all agents' knowledge simultaneously.