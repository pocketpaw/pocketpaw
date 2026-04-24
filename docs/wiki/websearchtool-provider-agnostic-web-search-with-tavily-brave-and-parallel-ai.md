---
{
  "title": "WebSearchTool: Provider-Agnostic Web Search with Tavily, Brave, and Parallel AI",
  "summary": "`WebSearchTool` routes web search queries to Tavily, Brave Search, or Parallel AI based on a settings-configured provider, with all three sharing a common `_format_results()` output shape. The provider selection is declarative — callers never specify the backend — keeping tool descriptions stable across deployment configurations.",
  "concepts": [
    "WebSearchTool",
    "Tavily",
    "Brave_Search",
    "Parallel_AI",
    "provider_routing",
    "result_formatting",
    "api_key_validation",
    "httpx",
    "num_results_clamping",
    "trust_level_standard"
  ],
  "categories": [
    "tools",
    "web-search",
    "information-retrieval",
    "provider-abstraction"
  ],
  "source_docs": [
    "9044ffb7795cd546"
  ],
  "backlinks": null,
  "word_count": 546,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`web_search.py` (Phase 1 Quick Wins, created 2026-02-06) implements the `web_search` tool, the most frequently invoked tool in PocketPaw's standard suite. Its key design constraint is provider agnosticism: callers describe what they want (a search query), not how to get it. Which search API to use is a deployment configuration decision, not a per-call decision.

## Provider Routing

```python
provider = settings.web_search_provider

if provider == "tavily":
    return await self._search_tavily(query, num_results, settings.tavily_api_key)
elif provider == "brave":
    return await self._search_brave(query, num_results, settings.brave_search_api_key)
elif provider == "parallel":
    return await self._search_parallel(query, num_results, settings.parallel_api_key)
else:
    return self._error(f"Unknown search provider '{provider}'.")
```

Three providers address different deployment scenarios:
- **Tavily**: Purpose-built for LLM agents. Returns clean, AI-optimized result snippets. Good default.
- **Brave Search**: Privacy-focused, independent index. Useful when Tavily is rate-limited or for privacy-sensitive deployments.
- **Parallel AI**: Combines search with content extraction. More expensive but higher-quality results.

The fallthrough `else` branch returns an explicit error for misconfigured providers rather than defaulting silently to one — this surfaces configuration mistakes immediately.

## Result Count Clamping

```python
num_results = min(max(num_results, 1), 10)
```

The result count is clamped to `[1, 10]` before the provider call. Without this, a caller passing `num_results=0` or `num_results=1000` would either get an API error or a very large response. The schema declares `default: 5, max: 10` but doesn't enforce the max at the JSON Schema level — the clamping in `execute()` is the actual enforcement.

## Common Output Format

All three `_search_*` methods call `_format_results(query, results)`, which produces a consistent markdown format regardless of which provider was used. Each result entry includes title, URL, and a snippet. This uniformity means downstream consumers (like `ResearchTool`) can parse `web_search` output without knowing which provider was active.

## API Key Validation

Each provider method checks for a missing API key before making any HTTP request:

```python
if not api_key:
    return self._error("Tavily API key not configured. Set TAVILY_API_KEY.")
```

The error message names the specific environment variable, so a user seeing the error knows exactly what to set. This pattern appears in all three provider methods — the key name differs (`TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, `PARALLEL_API_KEY`) but the pattern is identical.

## httpx Async Client

All three methods use `httpx.AsyncClient` in an `async with` context manager, ensuring the connection is properly closed after each request. The default timeout isn't shown in the excerpt, but `httpx`'s default (5 seconds for connection, no read timeout) would be too short for search APIs — the full implementation likely sets an explicit timeout.

## Trust Level: Standard

Web search is `standard` trust — agents can invoke it freely without elevated permissions. The tool reads from the public internet and returns text, posing no modification risk to local state. The main security concern (SSRF to private endpoints) doesn't apply because search APIs are queried directly without agent-controlled URL targeting.

## Known Gaps

- No result caching: identical queries within a session will re-hit the API each time.
- `_search_parallel` implementation details were not fully shown — its result schema may differ from Tavily/Brave.
- No fallback across providers: if Tavily is down and provider is set to `tavily`, the tool fails with no automatic retry via Brave.
- Snippet length and content quality vary significantly across providers but the schema gives no indication of this to callers.
