"""Wiki store — file-based CRUD for .md articles + index.json.

Storage layout per scope:
    ~/.pocketpaw/knowledge/{scope}/
    ├── raw/           # RawDoc JSON files
    ├── wiki/          # WikiArticle .md files with YAML frontmatter
    └── index.json     # KnowledgeIndex
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pocketpaw.knowledge.models import KnowledgeIndex, RawDoc, WikiArticle

logger = logging.getLogger(__name__)


class WikiStore:
    """File-based store for raw docs and compiled wiki articles."""

    def __init__(self, scope: str, base_path: str | None = None) -> None:
        self.scope = scope
        if base_path:
            self.root = Path(base_path) / _sanitize(scope)
        else:
            from pocketpaw.config import get_config_dir

            self.root = get_config_dir() / "knowledge" / _sanitize(scope)
        self.raw_dir = self.root / "raw"
        self.wiki_dir = self.root / "wiki"
        self.index_path = self.root / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

    # ── Raw docs ──

    def save_raw(self, doc: RawDoc) -> Path:
        """Save a raw document (metadata + text) as JSON."""
        path = self.raw_dir / f"{doc.id}.json"
        data = doc.to_dict()
        data["raw_text"] = doc.raw_text  # include full text for recompilation
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def load_raw(self, doc_id: str) -> RawDoc | None:
        path = self.raw_dir / f"{doc_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return RawDoc(
            id=data["id"],
            source_type=data["source_type"],
            source=data["source"],
            filename=data.get("filename"),
            content_type=data.get("content_type", "text"),
            raw_text=data.get("raw_text", ""),
            metadata=data.get("metadata", {}),
        )

    def list_raw(self) -> list[dict]:
        """List all raw docs (metadata only, no full text)."""
        docs = []
        for f in sorted(self.raw_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data.pop("raw_text", None)
                docs.append(data)
            except Exception:
                continue
        return docs

    def delete_raw(self, doc_id: str) -> None:
        path = self.raw_dir / f"{doc_id}.json"
        path.unlink(missing_ok=True)

    # ── Wiki articles ──

    def save_article(self, article: WikiArticle) -> Path:
        """Save a wiki article as .md with YAML frontmatter."""
        path = self.wiki_dir / f"{article.id}.md"
        frontmatter = {
            "title": article.title,
            "summary": article.summary,
            "concepts": article.concepts,
            "categories": article.categories,
            "source_docs": article.source_docs,
            "backlinks": article.backlinks,
            "word_count": article.word_count,
            "compiled_at": article.compiled_at.isoformat(),
            "compiled_with": article.compiled_with,
            "version": article.version,
        }
        md = f"---\n{json.dumps(frontmatter, indent=2, default=str)}\n---\n\n{article.content}"
        path.write_text(md, encoding="utf-8")
        return path

    def load_article(self, article_id: str) -> WikiArticle | None:
        path = self.wiki_dir / f"{article_id}.md"
        if not path.exists():
            return None
        return _parse_article(article_id, path.read_text(encoding="utf-8"))

    def list_articles(self) -> list[WikiArticle]:
        articles = []
        for f in sorted(self.wiki_dir.glob("*.md")):
            article = _parse_article(f.stem, f.read_text(encoding="utf-8"))
            if article:
                articles.append(article)
        return articles

    def delete_article(self, article_id: str) -> None:
        path = self.wiki_dir / f"{article_id}.md"
        path.unlink(missing_ok=True)

    # ── Index ──

    def load_index(self) -> KnowledgeIndex:
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                return KnowledgeIndex.from_dict(data)
            except Exception:
                logger.warning("Failed to load index, creating fresh")
        return KnowledgeIndex(scope=self.scope)

    def save_index(self, index: KnowledgeIndex) -> None:
        self.index_path.write_text(
            json.dumps(index.to_dict(), indent=2, default=str), encoding="utf-8"
        )

    # ── Stats ──

    def stats(self) -> dict:
        articles = self.list_articles()
        raw_docs = list(self.raw_dir.glob("*.json"))
        total_words = sum(a.word_count for a in articles)
        index = self.load_index()
        return {
            "articles": len(articles),
            "raw_docs": len(raw_docs),
            "words": total_words,
            "concepts": len(index.concepts),
            "categories": len(index.categories),
            "scope": self.scope,
        }

    # ── Clear ──

    def clear(self) -> None:
        import shutil

        if self.root.exists():
            shutil.rmtree(self.root)
        self._ensure_dirs()


# ── Helpers ──


def _sanitize(scope: str) -> str:
    """Sanitize scope string for use as directory name."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", scope)


def _parse_article(article_id: str, text: str) -> WikiArticle | None:
    """Parse a .md file with JSON frontmatter into a WikiArticle."""
    try:
        if not text.startswith("---"):
            return WikiArticle(
                id=article_id,
                title=article_id,
                summary="",
                content=text,
                word_count=len(text.split()),
            )

        parts = text.split("---", 2)
        if len(parts) < 3:
            return WikiArticle(
                id=article_id,
                title=article_id,
                summary="",
                content=text,
                word_count=len(text.split()),
            )

        fm = json.loads(parts[1])
        content = parts[2].strip()

        return WikiArticle(
            id=article_id,
            title=fm.get("title", article_id),
            summary=fm.get("summary", ""),
            content=content,
            concepts=fm.get("concepts", []),
            categories=fm.get("categories", []),
            source_docs=fm.get("source_docs", []),
            backlinks=fm.get("backlinks", []),
            word_count=fm.get("word_count", len(content.split())),
            compiled_with=fm.get("compiled_with", ""),
            version=fm.get("version", 1),
        )
    except Exception:
        logger.debug("Failed to parse article %s", article_id)
        return None
