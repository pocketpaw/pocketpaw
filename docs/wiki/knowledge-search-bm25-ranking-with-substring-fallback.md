---
{
  "title": "Knowledge Search: BM25 Ranking with Substring Fallback",
  "summary": "PocketPaw's knowledge search uses the `bm25s` library to rank wiki articles by relevance to a query, with a simple substring-match fallback when `bm25s` is not installed. The design deliberately avoids vector embeddings — LLM-compiled articles are semantically rich enough for BM25 to outperform naive chunked RAG.",
  "concepts": [
    "BM25",
    "bm25s",
    "search_articles",
    "_bm25_search",
    "_fallback_search",
    "searchable_text",
    "zero-score filtering",
    "no embeddings",
    "WikiArticle",
    "knowledge retrieval"
  ],
  "categories": [
    "knowledge",
    "search",
    "information retrieval"
  ],
  "source_docs": [
    "b8210dcee9ebc328"
  ],
  "backlinks": null,
  "word_count": 398,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/knowledge/search.py` implements the retrieval layer of PocketPaw's knowledge engine. The module's design philosophy is stated explicitly in its docstring: BM25 over LLM-compiled articles is a superior alternative to embedding-based RAG for this use case.

## Why BM25 Over Vectors?

Traditional RAG chunks raw documents into fixed-size pieces and embeds them. Retrieval quality depends on whether the relevant content landed near chunk boundaries. PocketPaw inverts this: the LLM compiles the raw document into a coherent article with structured sections, a summary, and an explicit concept list. BM25 over this structured text produces retrieval results comparable to or better than embeddings — at zero runtime ML cost.

The compile step is the investment. Search is cheap.

## BM25 Implementation

```python
def _bm25_search(articles: list[WikiArticle], query: str, limit: int) -> list[WikiArticle]:
    import bm25s

    corpus = [a.searchable_text() for a in articles]
    tokenized = bm25s.tokenize(corpus)
    retriever = bm25s.BM25()
    retriever.index(tokenized)

    query_tokens = bm25s.tokenize([query])
    k = min(limit, len(articles))
    results, scores = retriever.retrieve(query_tokens, corpus=corpus, k=k)

    ranked = []
    for doc_text, score in zip(results[0], scores[0]):
        if score <= 0:
            continue  # Filter zero-score non-matches
        ...
    return ranked
```

The index is rebuilt from the full article corpus on every call. For small-to-medium knowledge bases (hundreds of articles), `bm25s` indexing is fast enough that per-query rebuilds have negligible overhead.

## Fallback: Substring Matching

```python
def _fallback_search(articles: list[WikiArticle], query: str, limit: int) -> list[WikiArticle]:
    query_lower = query.lower()
    matches = [a for a in articles if query_lower in a.searchable_text().lower()]
    return matches[:limit]
```

The fallback uses simple substring containment — no ranking, no relevance scoring. It exists to avoid a hard dependency on `bm25s` at import time.

## Zero-Score Filtering

BM25 returns scores for all documents in the corpus, including those with zero relevance. The result loop skips any `score <= 0` to avoid returning unrelated articles in the top-k. Without this, every query would return exactly `limit` results even when most articles have no match.

## Public Interface

`search_articles(articles, query, limit)` is the only public function. It handles the try/except around the `bm25s` import and delegates to either backend. Callers never need to know which is active.

## Known Gaps

- **Index rebuilt per query**: For knowledge bases with 1000+ articles, per-query BM25 indexing may introduce latency. A persistent cached index would resolve this.
- **No query expansion**: Synonyms and related terms are not expanded. A query for "OAuth" will not match an article that only uses "token authentication".