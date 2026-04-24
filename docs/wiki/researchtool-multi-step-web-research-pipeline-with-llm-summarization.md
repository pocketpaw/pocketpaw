---
{
  "title": "ResearchTool: Multi-Step Web Research Pipeline with LLM Summarization",
  "summary": "`ResearchTool` orchestrates a four-stage pipeline — web search, URL discovery, content extraction, and LLM summarization — into a single `research` tool call. Depth levels let callers trade speed against thoroughness, and an optional memory-save step persists findings across conversations.",
  "concepts": [
    "ResearchTool",
    "WebSearchTool",
    "UrlExtractTool",
    "LLMRouter",
    "depth_levels",
    "memory_persistence",
    "pipeline_pattern",
    "graceful_degradation",
    "url_extraction",
    "summarization"
  ],
  "categories": [
    "tools",
    "research",
    "pipeline",
    "llm-integration"
  ],
  "source_docs": [
    "f6b7438e5262b20a"
  ],
  "backlinks": null,
  "word_count": 478,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`research.py` (Phase 2 Integration Ecosystem) implements a composite tool that chains three existing primitives: `WebSearchTool`, `UrlExtractTool`, and `LLMRouter`. The goal is to give agents the ability to conduct structured research in a single invocation rather than manually orchestrating multiple tool calls and stitching results together.

## Depth Levels

```python
_DEPTH_SOURCES = {
    "quick": 3,
    "standard": 5,
    "deep": 10,
}
```

The depth map is a deliberate design choice. Rather than exposing raw integer counts, the tool offers three named tiers that map to distinct user intents: `quick` for rapid lookups, `standard` for balanced research, and `deep` for comprehensive coverage. This prevents off-by-one misuse and gives the LLM a semantic vocabulary it can reason about when selecting depth.

## The Four-Stage Pipeline

```python
async def execute(self, topic, depth="standard", save_to_memory=False) -> str:
    # Stage 1: Web Search
    search_results = await WebSearchTool().execute(query=topic, num_results=num_sources)
    # Stage 2: URL extraction from results
    urls = self._extract_urls(search_results)
    # Stage 3: Content extraction
    extracted = await UrlExtractTool().execute(urls=urls[:num_sources])
    # Stage 4: LLM summarization
    summary = await self._summarize(topic, search_results, extracted)
```

**Stage 1** fires `WebSearchTool` which itself is provider-agnostic (Tavily/Brave/Parallel). If the search returns an error string, the pipeline aborts early rather than proceeding with no data.

**Stage 2** uses a regex-based URL extractor with deduplication and trailing-punctuation stripping. The stripping step (`url.rstrip(".,;:)")`) exists because search result snippets often embed URLs inside sentences, leaving trailing punctuation attached. Without stripping, those URLs would 404 when fetched.

**Stage 3** attempts content extraction from the top N URLs. If extraction fails entirely, the pipeline gracefully degrades: it continues with an empty `extracted` string and relies on the search-result snippets alone.

**Stage 4** passes both search results (capped at 3,000 chars) and extracted content (capped at 5,000 chars) to the LLM with a structured prompt requesting key findings, a detailed summary, and a source list.

## Graceful Degradation

The `_summarize` method has its own `except` handler that falls back to returning raw search and extracted text when the LLM call fails. This matters in low-connectivity or rate-limited environments — the user still gets useful information even if summarization is unavailable.

## Optional Memory Persistence

```python
if save_to_memory:
    manager = get_memory_manager()
    await manager.remember(f"Research on '{topic}':\n{summary[:2000]}", tags=["research", ...])
```

The `save_to_memory` flag lets agents persist research findings to long-term memory so they survive session boundaries. The summary is truncated to 2,000 characters before storage to avoid overwhelming the memory store. Memory failures are caught and logged as warnings — a failed save doesn't invalidate the research output.

## Known Gaps

- URL extraction is regex-based and will miss relative URLs or JavaScript-rendered links.
- The pipeline runs stages sequentially; parallel URL extraction would reduce latency for `deep` depth.
- No deduplication of extracted content across URLs — if two pages quote the same material, it's counted twice.
- `save_to_memory` silently truncates the summary to 2,000 characters with no indication to the user.
