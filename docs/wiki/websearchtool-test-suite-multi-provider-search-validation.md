---
{
  "title": "WebSearchTool Test Suite: Multi-Provider Search Validation",
  "summary": "This test module validates the `WebSearchTool` built-in tool in PocketPaw, covering provider dispatch, API key guards, HTTP error handling, result clamping, and the parallel-search variant. It ensures the tool behaves safely and predictably across Tavily, Brave, and Parallel AI backends.",
  "concepts": [
    "WebSearchTool",
    "web_search_provider",
    "Tavily",
    "Brave Search",
    "Parallel AI",
    "httpx",
    "API key validation",
    "result clamping",
    "AsyncMock",
    "provider dispatch",
    "tool trust level"
  ],
  "categories": [
    "testing",
    "web search",
    "tools",
    "built-in tools",
    "test"
  ],
  "source_docs": [
    "b9059a2ef91ebea7"
  ],
  "backlinks": null,
  "word_count": 535,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_web_search.py` is the primary test file for `pocketpaw.tools.builtin.web_search.WebSearchTool`. The tool allows PocketPaw agents to issue web searches through configurable providers. The tests exist to prevent regressions in provider dispatch, API credential validation, HTTP error propagation, and result count enforcement.

## Tool Identity and Schema

Two simple property tests—`test_name` and `test_trust_level`—anchor the tool's contract:

- `tool.name == "web_search"` ensures stable routing from the tool registry.
- `tool.trust_level == "standard"` prevents accidental privilege escalation; tools at higher trust levels bypass certain safety gates.

`test_parameters_schema` verifies that both `query` (required) and `num_results` (optional) appear in the JSON Schema, keeping the agent's function-call protocol consistent.

## Provider Dispatch

The tool reads `web_search_provider` from settings to choose a backend. Three providers are tested:

- **Tavily**: Uses `httpx.AsyncClient.post` against the Tavily API. `test_tavily_search_success` verifies that title and URL surface in the returned string.
- **Brave**: Uses `httpx.AsyncClient.get` against the Brave Search API. Response shape differs—results live under `web.results`—and the test confirms the adapter handles that path correctly.
- **Parallel AI**: Uses `httpx.AsyncClient.post` with custom headers (`x-api-key` and a `parallel-beta` header). `test_parallel_search_success` asserts both that results are returned and that the header values are forwarded correctly. This is the most explicit contract check in the suite.

Each provider test patches `httpx.AsyncClient` as an async context manager (`__aenter__`/`__aexit__`) to avoid real network calls.

## API Key Guards

Missing credentials should fail fast with a clear error rather than an opaque HTTP exception. Four tests cover this:

- `test_missing_tavily_api_key` — `tavily_api_key=None` must return an error string mentioning "Tavily API key".
- `test_missing_brave_api_key` — same pattern for `brave_search_api_key=None`.
- `test_parallel_missing_api_key` — same for `parallel_api_key=None`.
- `test_unknown_provider` — a provider name not in the dispatch table (e.g., `"duckduckgo"`) must return "Unknown search provider". This prevents silent fallthrough when misconfigured.

These guards matter because a missing key would otherwise cause a runtime `AttributeError` or an opaque 401 from the upstream API, making the failure hard to diagnose.

## HTTP Error Handling

`test_http_error` simulates a 401 Unauthorized from Tavily by having `raise_for_status` raise `httpx.HTTPStatusError`. The test asserts the tool returns an error string rather than propagating the exception, which would otherwise crash the agent's tool-execution loop.

## Empty Result Handling

`test_no_results` and `test_parallel_no_results` cover the case where the API returns a valid 200 response but an empty `results` list. Without explicit handling the tool would return an empty or malformed string. The tests confirm "No results found" is returned.

## Result Count Clamping

`test_num_results_clamped` passes `num_results=50` and expects the tool to silently cap it at 10 before forwarding to the API. This prevents agents from requesting arbitrarily large result sets that could inflate latency and API costs.

## Fixture Design

The `tool` fixture creates a fresh `WebSearchTool()` for each test, ensuring no shared state. Settings are always patched via `@patch("pocketpaw.tools.builtin.web_search.get_settings")`, which isolates tests from the developer's local `.env`.

## Known Gaps

- No test covers a network-level timeout (`httpx.TimeoutException`); the tool may not handle slow upstream APIs gracefully.
- The Parallel AI provider test hard-codes the `parallel-beta` header value as a substring check but does not assert the exact value, leaving room for a silent version mismatch.
- There is no test for concurrent invocations of the tool, so thread/async-safety under parallel agent calls is unverified.
