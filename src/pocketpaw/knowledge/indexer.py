"""Knowledge indexer — builds concept graph, backlinks, and categories from wiki articles.

Runs after compilation to maintain the index.json:
- Concepts: entities/terms → which articles mention them
- Backlinks: article A mentions article B's title → bidirectional link
- Categories: unique set across all articles
"""

from __future__ import annotations

import logging

from pocketpaw.knowledge.models import Concept, KnowledgeIndex, WikiArticle

logger = logging.getLogger(__name__)


def rebuild_index(scope: str, articles: list[WikiArticle]) -> KnowledgeIndex:
    """Rebuild the entire index from scratch from a list of articles."""
    index = KnowledgeIndex(scope=scope)

    # Collect all concepts
    concept_map: dict[str, Concept] = {}
    all_categories: set[str] = set()

    for article in articles:
        # Article metadata (no content — just for index)
        index.articles[article.id] = article.to_dict()

        # Concepts
        for concept_name in article.concepts:
            key = concept_name.lower().strip()
            if key not in concept_map:
                concept_map[key] = Concept(name=concept_name)
            if article.id not in concept_map[key].articles:
                concept_map[key].articles.append(article.id)

        # Categories
        for cat in article.categories:
            all_categories.add(cat)

    # Build backlinks — if article A's content mentions article B's title
    article_titles = {a.id: a.title.lower() for a in articles}
    for article in articles:
        content_lower = article.content.lower()
        for other_id, other_title in article_titles.items():
            if other_id == article.id:
                continue
            # Check if this article mentions another article's title
            if other_title and len(other_title) > 3 and other_title in content_lower:
                if other_id not in article.backlinks:
                    article.backlinks.append(other_id)
                # Bidirectional
                other_article = next((a for a in articles if a.id == other_id), None)
                if other_article and article.id not in other_article.backlinks:
                    other_article.backlinks.append(article.id)

    # Assign categories to concepts based on co-occurrence
    for concept in concept_map.values():
        cat_counts: dict[str, int] = {}
        for aid in concept.articles:
            article_meta = index.articles.get(aid, {})
            for cat in article_meta.get("categories", []):
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if cat_counts:
            concept.category = max(cat_counts, key=cat_counts.get)  # type: ignore[arg-type]

    index.concepts = concept_map
    index.categories = sorted(all_categories)

    logger.info(
        "Index rebuilt for %s: %d articles, %d concepts, %d categories",
        scope,
        len(articles),
        len(concept_map),
        len(all_categories),
    )
    return index


def update_index(index: KnowledgeIndex, article: WikiArticle) -> KnowledgeIndex:
    """Incrementally update the index with a new/updated article."""
    # Update article metadata
    index.articles[article.id] = article.to_dict()

    # Update concepts
    for concept_name in article.concepts:
        key = concept_name.lower().strip()
        if key not in index.concepts:
            index.concepts[key] = Concept(name=concept_name)
        if article.id not in index.concepts[key].articles:
            index.concepts[key].articles.append(article.id)

    # Update categories
    for cat in article.categories:
        if cat not in index.categories:
            index.categories.append(cat)
            index.categories.sort()

    return index


def remove_from_index(index: KnowledgeIndex, article_id: str) -> KnowledgeIndex:
    """Remove an article from the index."""
    index.articles.pop(article_id, None)

    # Remove from concepts
    empty_concepts = []
    for key, concept in index.concepts.items():
        if article_id in concept.articles:
            concept.articles.remove(article_id)
        if not concept.articles:
            empty_concepts.append(key)
    for key in empty_concepts:
        del index.concepts[key]

    return index
