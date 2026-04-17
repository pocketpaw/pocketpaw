"""Knowledge engine data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RawDoc:
    """An ingested raw document — preserved for recompilation."""

    id: str  # content hash
    source_type: str  # file, url, text, repo
    source: str  # file path, URL, or "manual"
    filename: str | None = None
    content_type: str = "text"  # pdf, html, markdown, image, csv, etc.
    raw_text: str = ""
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source": self.source,
            "filename": self.filename,
            "content_type": self.content_type,
            "word_count": len(self.raw_text.split()),
            "ingested_at": self.ingested_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class WikiArticle:
    """A compiled knowledge article — the unit of search."""

    id: str  # slug derived from title
    title: str
    summary: str  # 2-3 sentence summary
    content: str  # full compiled markdown
    concepts: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    source_docs: list[str] = field(default_factory=list)  # RawDoc IDs
    backlinks: list[str] = field(default_factory=list)  # other article IDs
    word_count: int = 0
    compiled_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    compiled_with: str = ""  # model used
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "concepts": self.concepts,
            "categories": self.categories,
            "source_docs": self.source_docs,
            "backlinks": self.backlinks,
            "word_count": self.word_count,
            "compiled_at": self.compiled_at.isoformat(),
            "compiled_with": self.compiled_with,
            "version": self.version,
        }

    def searchable_text(self) -> str:
        """Full text for BM25 indexing — title + summary + content + concepts."""
        parts = [self.title, self.summary, self.content]
        parts.extend(self.concepts)
        parts.extend(self.categories)
        return " ".join(parts)


@dataclass
class Concept:
    """A key entity/topic that appears across articles."""

    name: str
    articles: list[str] = field(default_factory=list)  # WikiArticle IDs
    category: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "articles": self.articles, "category": self.category}


@dataclass
class LintIssue:
    """A problem found by the knowledge linter."""

    type: str  # inconsistency, gap, suggestion, connection
    severity: str  # info, warning, error
    message: str
    article_id: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "article_id": self.article_id,
            "suggestion": self.suggestion,
        }


@dataclass
class KnowledgeIndex:
    """The master index for a knowledge scope."""

    scope: str
    articles: dict[str, dict] = field(default_factory=dict)  # id → article metadata (no content)
    concepts: dict[str, Concept] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "articles": self.articles,
            "concepts": {k: v.to_dict() for k, v in self.concepts.items()},
            "categories": self.categories,
        }

    @staticmethod
    def from_dict(data: dict) -> KnowledgeIndex:
        concepts = {}
        for k, v in data.get("concepts", {}).items():
            concepts[k] = Concept(
                name=v["name"], articles=v.get("articles", []), category=v.get("category")
            )
        return KnowledgeIndex(
            scope=data.get("scope", ""),
            articles=data.get("articles", {}),
            concepts=concepts,
            categories=data.get("categories", []),
        )
