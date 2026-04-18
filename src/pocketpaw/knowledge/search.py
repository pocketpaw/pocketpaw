"""Knowledge search — BM25 keyword search over compiled wiki articles.

No vector DB, no embeddings. BM25 over well-structured LLM-compiled articles
is highly effective because the compile step already does semantic understanding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw.knowledge.models import WikiArticle

logger = logging.getLogger(__name__)


def search_articles(
    articles: list[WikiArticle],
    query: str,
    limit: int = 5,
) -> list[WikiArticle]:
    """BM25 search over wiki articles. Returns ranked results.

    Falls back to simple substring matching if bm25s is not installed.
    """
    if not articles or not query.strip():
        return []

    # Try BM25
    try:
        return _bm25_search(articles, query, limit)
    except ImportError:
        logger.debug("bm25s not installed, falling back to substring search")
        return _fallback_search(articles, query, limit)


def _bm25_search(articles: list[WikiArticle], query: str, limit: int) -> list[WikiArticle]:
    """BM25 search using bm25s library."""
    import bm25s

    # Build corpus from searchable text of each article
    corpus = [a.searchable_text() for a in articles]

    tokenized = bm25s.tokenize(corpus)
    retriever = bm25s.BM25()
    retriever.index(tokenized)

    query_tokens = bm25s.tokenize([query])
    k = min(limit, len(articles))
    results, scores = retriever.retrieve(query_tokens, corpus=corpus, k=k)

    # Map back to articles, filter out zero-score results
    ranked = []
    for doc_text, score in zip(results[0], scores[0]):
        if score <= 0:
            continue
        # Find the article that matches this corpus entry
        idx = corpus.index(doc_text) if doc_text in corpus else -1
        if idx >= 0:
            ranked.append(articles[idx])

    return ranked


def _fallback_search(articles: list[WikiArticle], query: str, limit: int) -> list[WikiArticle]:
    """Simple substring matching fallback when bm25s is not available."""
    query_lower = query.lower()
    terms = query_lower.split()

    scored = []
    for article in articles:
        text = article.searchable_text().lower()
        # Score = number of query terms found + bonus for title/concept matches
        score = sum(1 for t in terms if t in text)
        if article.title.lower().find(query_lower) >= 0:
            score += 5
        for concept in article.concepts:
            if query_lower in concept.lower():
                score += 3
        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [a for _, a in scored[:limit]]
