---
{
  "title": "Knowledge Indexer: Concept Graph and Backlink Builder",
  "summary": "The knowledge indexer maintains a `KnowledgeIndex` that maps concepts to the articles mentioning them and builds bidirectional backlinks between articles that reference each other's titles. It supports both full rebuilds from scratch and incremental updates when a single article is added or removed.",
  "concepts": [
    "KnowledgeIndex",
    "concept graph",
    "backlinks",
    "rebuild_index",
    "update_index",
    "remove_from_index",
    "Concept",
    "WikiArticle",
    "incremental indexing",
    "cross-article discovery"
  ],
  "categories": [
    "knowledge",
    "search",
    "graph"
  ],
  "source_docs": [
    "4b33eedf47d6f813"
  ],
  "backlinks": null,
  "word_count": 309,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/indexer.py` runs after compilation to maintain the knowledge graph. It processes all `WikiArticle` objects and produces a `KnowledgeIndex` that enables concept-based navigation and cross-article discovery — functionality that BM25 keyword search alone cannot provide.

## Three Index Structures

The `KnowledgeIndex` built by the indexer has three main structures:

| Structure | Key | Value |
|-----------|-----|-------|
| `articles` | article ID | article metadata dict |
| `concepts` | concept name (lowercased) | `Concept` (article IDs mentioning it) |
| `categories` | category name | article IDs in that category |

These enable queries like "which articles mention 'revenue'?" without a full-text scan.

## Concept Graph Construction

```python
for article in articles:
    for concept_name in article.concepts:
        key = concept_name.lower().strip()
        if key not in concept_map:
            concept_map[key] = Concept(name=concept_name, article_ids=[])
        concept_map[key].article_ids.append(article.id)
```

Concepts are lowercased for deduplication — "Revenue" and "revenue" map to the same concept node. This prevents fragmentation when the LLM capitalizes inconsistently.

## Backlink Discovery

After all concepts are indexed, the indexer scans article titles against each article's content to detect cross-references. If article A's content mentions article B's title (simple substring match), A gets a backlink to B. This enables wiki-style "see also" links without manual authoring.

## Incremental vs. Full Rebuild

```python
def rebuild_index(scope, articles) -> KnowledgeIndex:
    # Process all articles from scratch

def update_index(index, article) -> KnowledgeIndex:
    # Remove old entry for this article, add new one

def remove_from_index(index, article_id) -> KnowledgeIndex:
    # Remove all references to this article
```

`rebuild_index` is used on `recompile_all()`. `update_index` is used for incremental ingestion, avoiding reprocessing the entire corpus.

## Known Gaps

- **Backlink detection is title-only**: The indexer only detects backlinks when article B's exact title appears in article A's content. Concept-based linking is not implemented.
- **No weighted concepts**: All concept mentions are treated equally. Frequently mentioned concepts are not distinguished from single-mention concepts.