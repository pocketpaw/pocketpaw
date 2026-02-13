# Research Pipeline tool — chains WebSearch + UrlExtract + LLM summarization.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

import logging
from typing import Any

from pocketclaw.config import Settings
from pocketclaw.llm.router import LLMRouter
from pocketclaw.tools.builtin.url_extract import UrlExtractTool
from pocketclaw.tools.builtin.web_search import WebSearchTool
from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# Depth levels: number of sources to consult
_DEPTH_SOURCES = {
    "quick": 3,
    "standard": 5,
    "deep": 10,
}


class ResearchTool(BaseTool):
    """Multi-step research pipeline: search -> extract -> summarize."""

    @property
    def name(self) -> str:
        return "research"

    @property
    def description(self) -> str:
        return (
            "Conduct multi-step research on a topic. Searches the web, extracts "
            "content from top results, and produces a structured summary using LLM. "
            "Depth levels: quick (3 sources), standard (5), deep (10). "
            "Can optionally save the summary to long-term memory."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Research topic or question",
                },
                "depth": {
                    "type": "string",
                    "description": (
                        "Research depth: 'quick', 'standard', or 'deep' (default: standard)"
                    ),
                    "enum": ["quick", "standard", "deep"],
                    "default": "standard",
                },
                "save_to_memory": {
                    "type": "boolean",
                    "description": "Save research summary to long-term memory (default: false)",
                    "default": False,
                },
            },
            "required": ["topic"],
        }

    async def execute(
        self,
        topic: str,
        depth: str = "standard",
        save_to_memory: bool = False,
    ) -> str:
        num_sources = _DEPTH_SOURCES.get(depth, 5)

        try:
            # Step 1: Web Search
            search_tool = WebSearchTool()
            search_results = await search_tool.execute(query=topic, num_results=num_sources)

            if search_results.startswith("Error"):
                return self._error(f"Search failed: {search_results}")

            # Step 2: Extract URLs from search results
            urls = self._extract_urls(search_results)
            if not urls:
                return f"**Research: {topic}**\n\n{search_results}\n\n(No URLs to extract)"

            # Step 3: Extract content from top URLs
            extract_tool = UrlExtractTool()
            extracted = await extract_tool.execute(urls=urls[:num_sources])

            if extracted.startswith("Error"):
                # Fall back to search results only
                extracted = ""

            # Step 4: LLM Summarization
            summary = await self._summarize(topic, search_results, extracted)

            # Step 5: Optionally save to memory
            if save_to_memory:
                try:
                    from pocketclaw.memory import get_memory_manager

                    manager = get_memory_manager()
                    await manager.remember(
                        f"Research on '{topic}':\n{summary[:2000]}",
                        tags=["research", topic.split()[0].lower()],
                    )
                    summary += "\n\n*Summary saved to memory.*"
                except Exception as e:
                    logger.warning("Failed to save research to memory: %s", e)

            return summary

        except Exception as e:
            return self._error(f"Research failed: {e}")

    @staticmethod
    def _extract_urls(search_results: str) -> list[str]:
        """Extract URLs from formatted search results."""
        import re

        urls = re.findall(r"https?://[^\s\n]+", search_results)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for url in urls:
            # Strip trailing punctuation
            url = url.rstrip(".,;:)")
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    async def _summarize(self, topic: str, search_results: str, extracted: str) -> str:
        """Use LLM to produce a structured research summary."""
        try:
            settings = Settings.load()
            router = LLMRouter(settings)

            prompt = (
                f"Summarize the following research on the topic: '{topic}'\n\n"
                f"## Search Results\n{search_results[:3000]}\n\n"
            )
            if extracted:
                prompt += f"## Extracted Content\n{extracted[:5000]}\n\n"

            prompt += (
                "Produce a structured research summary with:\n"
                "1. **Key Findings** (3-5 bullet points)\n"
                "2. **Detailed Summary** (2-3 paragraphs)\n"
                "3. **Sources** (numbered list of URLs)\n"
            )

            summary = await router.chat(prompt)
            return f"**Research: {topic}**\n\n{summary}"

        except Exception as e:
            # Fallback: return raw search results without LLM summary
            logger.warning("LLM summarization failed, returning raw results: %s", e)
            return (
                f"**Research: {topic}**\n\n"
                f"## Search Results\n{search_results}\n\n"
                f"## Extracted Content\n{extracted[:3000] if extracted else '(none)'}"
            )
