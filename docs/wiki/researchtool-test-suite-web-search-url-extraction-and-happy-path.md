---
{
  "title": "ResearchTool Test Suite — Web Search, URL Extraction, and Happy Path",
  "summary": "This test file validates `ResearchTool`, PocketPaw's built-in agentic research capability that chains web search, URL content extraction, and LLM summarization. Tests cover tool metadata, URL parsing edge cases, search failure handling, and the full happy-path pipeline using mocked external dependencies.",
  "concepts": [
    "ResearchTool",
    "web search",
    "URL extraction",
    "LLM summarization",
    "WebSearchTool",
    "UrlExtractTool",
    "LLMRouter",
    "tool trust level",
    "tool definition",
    "search failure handling",
    "pipeline mocking"
  ],
  "categories": [
    "testing",
    "tools",
    "web search",
    "agent pipelines",
    "test"
  ],
  "source_docs": [
    "3409e5e056f47ec2"
  ],
  "backlinks": null,
  "word_count": 500,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_research.py` verifies the behavior of `ResearchTool` from `pocketpaw.tools.builtin.research`. The tool implements a multi-step research workflow: it searches the web for a topic, extracts URLs from the search results, fetches page content from each URL, and synthesizes a summary using the LLM router. All external I/O is mocked in tests to keep them fast and deterministic.

## Tool Definition Tests

`TestToolDefinition` asserts that the tool presents the correct contract to PocketPaw's tool registry:

- `name == "research"` — the slash command / tool call identifier.
- `trust_level == "standard"` — this is important; the tool accesses the network but does not write to disk or execute code, so it sits at the standard tier rather than elevated.
- `parameters` includes `topic`, `depth`, and `save_to_memory` — the three user-facing controls for the research pipeline.

These tests exist because tool metadata is machine-readable. If the name or trust level drifts, the routing, policy enforcement, and dashboard display all break silently.

## URL Extraction Edge Cases

`ResearchTool._extract_urls` is a private helper that parses markdown-formatted search results and pulls out bare URLs. `TestExtractUrls` covers:

- **Normal extraction** — two distinct URLs in a numbered list are found.
- **Deduplication** — the same URL appearing twice yields one entry. This matters because search APIs often return related links that share a canonical URL.
- **Trailing punctuation stripping** — `https://example.com/path.` must become `https://example.com/path`. Without this, the fetched URL returns a 404 and the research output silently loses a source.
- **No URLs present** — returns an empty list, not an error.

The punctuation-stripping case is defensive programming against how search APIs format results inline (e.g., "Visit https://example.com." with a period closing the sentence).

## Search Failure Handling

`test_research_search_failure` patches `WebSearchTool` to return a string beginning with `"Error: No API key"` and asserts that the research result propagates a `"Search failed"` message rather than crashing or returning empty output. This prevents the scenario where a missing API key causes the research pipeline to silently produce no output with no indication of why.

```python
async def test_research_search_failure():
    tool = ResearchTool()
    mock_search_tool = MagicMock()
    mock_search_tool.execute = AsyncMock(return_value="Error: No API key")
    with patch("pocketpaw.tools.builtin.research.WebSearchTool", return_value=mock_search_tool):
        result = await tool.execute(topic="quantum computing")
        assert "Error" in result
        assert "Search failed" in result
```

## Happy Path — Full Pipeline Mock

`test_research_happy_path` patches all three external collaborators simultaneously:

- `WebSearchTool` — returns formatted search results with one URL.
- `UrlExtractTool` — returns markdown content simulating a fetched page.
- `LLMRouter` — returns a synthesized summary string.
- `Settings.load` — returns a mock to avoid config file access.

The test verifies that the final output includes the topic title `"Research: quantum computing"`, confirming the pipeline ran end to end and the result is formatted as expected.

## Known Gaps

No `TODO` or `FIXME` markers are present. The test file does not cover `save_to_memory=True` behavior (writing the result to PocketPaw's memory store), nor does it test the `depth` parameter's effect on how many URLs are fetched and summarized. These represent untested branches in the production code.
