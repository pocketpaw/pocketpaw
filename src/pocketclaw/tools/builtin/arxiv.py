# Arxiv Search tool — search for papers on arXiv.
# Created: 2026-02-14

import logging
from typing import Any
import asyncio

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class ArxivTool(BaseTool):
    """Search for research papers on arXiv."""

    @property
    def name(self) -> str:
        return "arxiv"

    @property
    def description(self) -> str:
        return (
            "Search for research papers on arXiv. Returns a list of results "
            "with titles, authors, published dates, summaries, and PDF links. "
            "Useful for finding scientific papers and technical reports."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (e.g. 'LLM agents', 'quantum computing')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 10)",
                    "default": 5,
                },
                "sort_by": {
                    "type": "string",
                    "description": "Sort order: 'relevance', 'lastUpdatedDate', 'submittedDate' (default: relevance)",
                    "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                    "default": "relevance",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        max_results: int = 5,
        sort_by: str = "relevance",
    ) -> str:
        """Search arXiv for papers."""
        try:
            import arxiv
        except ImportError:
            return self._error(
                "arxiv package not installed. Install with: pip install 'pocketpaw[research]'"
            )

        max_results = min(max(max_results, 1), 10)
        
        # Map string to arxiv SortCriterion enum
        sort_criterion = arxiv.SortCriterion.Relevance
        if sort_by == "lastUpdatedDate":
            sort_criterion = arxiv.SortCriterion.LastUpdatedDate
        elif sort_by == "submittedDate":
            sort_criterion = arxiv.SortCriterion.SubmittedDate

        try:
            # arxiv client is synchronous, run in thread
            return await asyncio.to_thread(
                self._search_sync, query, max_results, sort_criterion
            )
        except Exception as e:
            return self._error(f"Arxiv search failed: {e}")

    def _search_sync(self, query: str, max_results: int, sort_criterion: Any) -> str:
        import arxiv

        client = arxiv.Client()
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=sort_criterion,
        )

        results = []
        for result in client.results(search):
            results.append(result)

        if not results:
            return f"No results found for: {query}"

        return self._format_results(query, results)

    def _format_results(self, query: str, results: list[Any]) -> str:
        lines = [f"Arxiv results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.title.replace("\n", " ")
            authors = ", ".join(a.name for a in r.authors[:3])
            if len(r.authors) > 3:
                authors += ", et al."
            published = r.published.strftime("%Y-%m-%d")
            summary = r.summary.replace("\n", " ")[:300] + "..."
            pdf_url = r.pdf_url
            
            lines.append(
                f"{i}. **{title}**\n"
                f"   Authors: {authors}\n"
                f"   Published: {published}\n"
                f"   PDF: {pdf_url}\n"
                f"   Summary: {summary}\n"
            )
        return "\n".join(lines)
