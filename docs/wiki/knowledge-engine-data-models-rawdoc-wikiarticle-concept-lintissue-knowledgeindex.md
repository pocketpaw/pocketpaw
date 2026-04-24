---
{
  "title": "Knowledge Engine Data Models: RawDoc, WikiArticle, Concept, LintIssue, KnowledgeIndex",
  "summary": "The `knowledge.models` module defines the five core dataclasses of PocketPaw's knowledge engine — `RawDoc` (ingested source), `WikiArticle` (compiled artifact), `Concept` (cross-article entity), `LintIssue` (audit finding), and `KnowledgeIndex` (master index). Each includes a `to_dict()` method for JSON serialization to disk.",
  "concepts": [
    "RawDoc",
    "WikiArticle",
    "Concept",
    "LintIssue",
    "KnowledgeIndex",
    "searchable_text",
    "to_dict",
    "from_dict",
    "dataclasses",
    "model_used",
    "knowledge schema"
  ],
  "categories": [
    "knowledge",
    "models",
    "data structures"
  ],
  "source_docs": [
    "392181acd18d4de5"
  ],
  "backlinks": null,
  "word_count": 386,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/models.py` is the data contract for the entire knowledge subsystem. Every other module in `pocketpaw.knowledge` imports from here. Using plain Python `dataclasses` rather than Pydantic keeps the models lightweight and free of validation overhead at the persistence layer.

## RawDoc: The Ingestion Record

```python
@dataclass
class RawDoc:
    id: str           # content hash (16 hex chars)
    source_type: str  # file | url | text | repo
    source: str       # file path, URL, or "manual"
    filename: str | None
    content_type: str  # pdf | html | markdown | image | csv | text
    raw_text: str     # full extracted text
    ingested_at: datetime
    metadata: dict    # word_count, page_count, etc.
```

`RawDoc` preserves the original text so articles can be recompiled when the compile prompt improves. The `to_dict()` method omits `raw_text` for the index (word count only), keeping the index lean.

## WikiArticle: The Unit of Search

```python
@dataclass
class WikiArticle:
    id: str           # slug derived from title
    title: str
    summary: str      # 2-3 sentences
    content: str      # full compiled markdown
    concepts: list[str]
    categories: list[str]
    source_docs: list[str]  # RawDoc IDs
    backlinks: list[str]    # article IDs that reference this one
    compiled_at: datetime
    model_used: str   # which LLM compiled this
```

The `model_used` field tracks which model compiled the article. This lets the system identify articles compiled with older models that might benefit from recompilation after a model upgrade.

```python
def searchable_text(self) -> str:
    return f"{self.title} {self.summary} {self.content} {' '.join(self.concepts)}"
```

`searchable_text()` is the BM25 input. Concatenating title and concepts into the search corpus means a query for a concept name matches even if the article body does not repeat the exact phrase.

## Concept: Cross-Article Entity Node

```python
@dataclass
class Concept:
    name: str
    article_ids: list[str]  # articles that mention this concept
```

Concepts are the nodes in the knowledge graph, enabling the query "which articles discuss 'OAuth'?" without scanning full article content.

## KnowledgeIndex: The Master Registry

```python
@dataclass
class KnowledgeIndex:
    scope: str
    articles: dict[str, dict]
    concepts: dict[str, Concept]
    categories: dict[str, list[str]]
    updated_at: datetime
```

`KnowledgeIndex.from_dict()` reconstructs the index from its JSON representation on disk, including rehydrating nested `Concept` objects. This round-trip fidelity is essential for the lazy-load pattern in `WikiStore`.

## Known Gaps

- **No schema versioning**: None of the dataclasses carry a `schema_version` field. A field rename or type change would silently corrupt existing indexes loaded from disk.