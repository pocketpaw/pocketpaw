"""PocketPaw Knowledge Engine — scope-agnostic knowledge management.

Usage:
    engine = KnowledgeEngine(scope="agent:abc123")
    engine = KnowledgeEngine(scope="workspace:ws1")
    engine = KnowledgeEngine(scope="pocket:p1")

    await engine.ingest_url("https://example.com/about")
    results = await engine.search("revenue projections")
    issues = await engine.lint()
"""

from __future__ import annotations

import logging

from pocketpaw.knowledge.models import KnowledgeIndex, LintIssue, RawDoc, WikiArticle  # noqa: F401
from pocketpaw.knowledge.store import WikiStore

logger = logging.getLogger(__name__)


class KnowledgeEngine:
    """Scope-agnostic knowledge engine with ingest → compile → index → search pipeline."""

    def __init__(self, scope: str, base_path: str | None = None) -> None:
        self.scope = scope
        self.store = WikiStore(scope, base_path)

    # ── Ingest ──

    async def ingest_text(self, text: str, source: str = "manual") -> WikiArticle:
        """Ingest plain text → store raw → compile → index → return article."""
        from pocketpaw.knowledge.ingest import ingest_text

        raw = await ingest_text(text, source)
        return await self._process_raw(raw)

    async def ingest_url(self, url: str) -> WikiArticle:
        """Fetch URL → store raw → compile → index → return article."""
        from pocketpaw.knowledge.ingest import ingest_url

        raw = await ingest_url(url)
        return await self._process_raw(raw)

    async def ingest_file(self, file_path: str) -> WikiArticle:
        """Read file → store raw → compile → index → return article."""
        from pocketpaw.knowledge.ingest import ingest_file

        raw = await ingest_file(file_path)
        return await self._process_raw(raw)

    async def ingest_files(self, file_paths: list[str]) -> list[WikiArticle]:
        """Batch ingest multiple files."""
        results = []
        for path in file_paths:
            try:
                article = await self.ingest_file(path)
                results.append(article)
            except Exception as exc:
                logger.warning("Failed to ingest %s: %s", path, exc)
        return results

    async def _process_raw(self, raw: RawDoc) -> WikiArticle:
        """Store raw doc → compile with LLM → index → save article.

        If LLM compilation fails, creates a basic article from raw text directly.
        Raw doc is always saved regardless of compilation success.
        """
        import re

        from pocketpaw.knowledge.indexer import update_index

        # Always save raw doc first
        self.store.save_raw(raw)
        logger.info(
            "Saved raw doc: %s (%s, %d words)", raw.id, raw.source, len(raw.raw_text.split())
        )

        # Try LLM compilation, fall back to basic article
        try:
            from pocketpaw.knowledge.compiler import compile_article

            article = await compile_article(raw)
        except Exception:
            logger.warning(
                "LLM compilation failed for %s, creating basic article", raw.id, exc_info=True
            )
            # Create basic article from raw text without LLM
            slug = re.sub(r"[^a-z0-9\s-]", "", (raw.filename or raw.source or raw.id).lower())
            slug = re.sub(r"[\s-]+", "-", slug)[:80].strip("-") or raw.id[:16]
            article = WikiArticle(
                id=slug,
                title=raw.filename or raw.source or "Untitled",
                summary=raw.raw_text[:200].strip(),
                content=raw.raw_text,
                concepts=[],
                categories=[],
                source_docs=[raw.id],
                word_count=len(raw.raw_text.split()),
                compiled_with="none (fallback)",
            )

        # Ensure unique ID
        existing = self.store.load_article(article.id)
        if existing:
            article.version = existing.version + 1

        # Save article
        article.word_count = len(article.content.split())
        self.store.save_article(article)

        # Update index
        index = self.store.load_index()
        index = update_index(index, article)
        self.store.save_index(index)

        logger.info("Ingested and compiled: %s (%d words)", article.title, article.word_count)
        return article

    # ── Search ──

    async def search(self, query: str, limit: int = 5) -> list[WikiArticle]:
        """BM25 search over compiled wiki articles."""
        from pocketpaw.knowledge.search import search_articles

        articles = self.store.list_articles()
        return search_articles(articles, query, limit)

    async def search_context(self, query: str, limit: int = 3, max_chars: int = 8000) -> str:
        """Search and return a formatted context block for injection into agent prompts."""
        results = await self.search(query, limit)
        if not results:
            return ""

        parts = []
        total = 0
        for article in results:
            # Use summary if article is too long
            text = (
                article.content
                if len(article.content) <= 2000
                else f"## {article.title}\n{article.summary}"
            )
            if total + len(text) > max_chars:
                break
            parts.append(f"## {article.title}\n{text}")
            total += len(text)

        return "\n\n---\n\n".join(parts)

    # ── Browse ──

    def list_articles(self) -> list[dict]:
        """List all articles (metadata, no full content)."""
        return [a.to_dict() for a in self.store.list_articles()]

    def get_article(self, article_id: str) -> WikiArticle | None:
        """Get a full article by ID."""
        return self.store.load_article(article_id)

    def list_concepts(self) -> list[dict]:
        """List all concepts with their article associations."""
        index = self.store.load_index()
        return [c.to_dict() for c in index.concepts.values()]

    def articles_by_concept(self, concept: str) -> list[WikiArticle]:
        """Get all articles that mention a concept."""
        index = self.store.load_index()
        key = concept.lower().strip()
        c = index.concepts.get(key)
        if not c:
            return []
        return [a for a in self.store.list_articles() if a.id in c.articles]

    def articles_by_category(self, category: str) -> list[WikiArticle]:
        """Get all articles in a category."""
        return [a for a in self.store.list_articles() if category in a.categories]

    def list_raw_docs(self) -> list[dict]:
        """List all raw documents (metadata only)."""
        return self.store.list_raw()

    # ── Lint ──

    async def lint(self, model: str = "", backend: str = "") -> list[LintIssue]:
        """Run LLM-powered health checks on the knowledge base."""
        from pocketpaw.knowledge.linter import lint_knowledge

        articles = self.store.list_articles()
        raw_docs = self.store.list_raw()
        index = self.store.load_index()
        return await lint_knowledge(articles, raw_docs, index, model, backend)

    # ── Recompile ──

    async def recompile(self, article_id: str) -> WikiArticle | None:
        """Recompile a single article from its source raw docs."""
        from pocketpaw.knowledge.compiler import compile_article
        from pocketpaw.knowledge.indexer import update_index

        article = self.store.load_article(article_id)
        if not article or not article.source_docs:
            return None

        # Load source raw docs and concatenate
        texts = []
        for doc_id in article.source_docs:
            raw = self.store.load_raw(doc_id)
            if raw:
                texts.append(raw.raw_text)
        if not texts:
            return None

        # Create a synthetic raw doc from combined sources
        combined = RawDoc(
            id=article.source_docs[0],
            source_type="recompile",
            source=f"recompile:{article_id}",
            raw_text="\n\n".join(texts),
        )

        new_article = await compile_article(combined)
        new_article.id = article_id  # Keep same ID
        new_article.version = article.version + 1
        new_article.source_docs = article.source_docs
        new_article.word_count = len(new_article.content.split())

        self.store.save_article(new_article)
        index = self.store.load_index()
        index = update_index(index, new_article)
        self.store.save_index(index)

        return new_article

    async def recompile_all(self) -> list[WikiArticle]:
        """Recompile all articles from their source raw docs."""
        articles = self.store.list_articles()
        results = []
        for article in articles:
            try:
                result = await self.recompile(article.id)
                if result:
                    results.append(result)
            except Exception as exc:
                logger.warning("Failed to recompile %s: %s", article.id, exc)
        # Rebuild full index
        from pocketpaw.knowledge.indexer import rebuild_index

        all_articles = self.store.list_articles()
        index = rebuild_index(self.scope, all_articles)
        self.store.save_index(index)
        return results

    # ── Stats ──

    def stats(self) -> dict:
        """Return knowledge base statistics."""
        return self.store.stats()

    # ── Clear ──

    def clear(self) -> None:
        """Delete all knowledge for this scope."""
        self.store.clear()
