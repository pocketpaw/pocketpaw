---
{
  "title": "Reddit Tool Tests: Search, Read, Trending, and Post Formatting",
  "summary": "PocketPaw's Reddit integration provides agents with three tools for interacting with Reddit: `reddit_search`, `reddit_read`, and `reddit_trending`. These tests validate tool schemas and trust levels, the `_format_post` helper for normalizing Reddit API responses, and async tool execution covering success, empty results, and error conditions.",
  "concepts": [
    "reddit_search",
    "reddit_read",
    "reddit_trending",
    "RedditClient",
    "_format_post",
    "deleted post handling",
    "rate limiting",
    "tool schema",
    "trust level",
    "async tool execution"
  ],
  "categories": [
    "testing",
    "integrations",
    "tool system",
    "test"
  ],
  "source_docs": [
    "7f0b68bbd986c51e"
  ],
  "backlinks": null,
  "word_count": 448,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Reddit integration allows PocketPaw agents to search subreddits, read post threads, and surface trending content. The integration uses `RedditClient` as an async HTTP wrapper around the Reddit API, and exposes three tool-use functions to LLMs. Rate limiting is built in to avoid API bans.

## Tool Schema Validation

`TestRedditToolSchemas` validates each tool's interface contract:

- **`reddit_search`**: requires `query`, optionally scopes to a `subreddit`. Trust level is `standard` — the tool reads public data and poses no write risk.
- **`reddit_read`**: requires `url` as a required parameter. This is strict: the LLM must provide a URL, not a post ID or subreddit path.
- **`reddit_trending`**: accepts `subreddit` and `time_filter` parameters, both optional.

Schema tests are fast, dependency-free, and catch the most common tool integration bug: a required parameter marked as optional, or an optional parameter marked as required, which causes LLM tool calls to fail at the JSON Schema validation layer.

## Post Formatting

`TestRedditClientFormatPost` validates `RedditClient._format_post()`, which normalizes raw Reddit API post dictionaries:

- **Standard post**: title, author, score, comment count, and URL are correctly extracted. The `permalink` (`/r/python/comments/abc/...`) is converted to a full `reddit.com` URL.
- **Deleted post**: an empty dict (returned by Reddit for deleted posts) produces `[deleted]` author and empty title rather than raising `KeyError`.

The deleted post case is critical — Reddit returns empty dicts for removed content, and a `KeyError` would crash the tool execution for a legitimate search that happens to include a deleted post.

## Async Tool Execution

The module-level async functions test end-to-end tool behavior with mocked `RedditClient` and rate limiter:

- **`test_reddit_search_success`**: mock posts are returned, output contains post title and score.
- **`test_reddit_search_no_results`**: empty results list produces "No posts found" message — not an empty string or `None`.
- **`test_reddit_read_success`**: post body and comment thread are included in output.
- **`test_reddit_trending_success`**: trending posts are formatted with score (note: `5,000` formatted with comma separator).
- **`test_reddit_trending_empty`**: empty trending list produces "No trending posts" message.
- **`test_reddit_read_error`**: when the API returns `{"error": "..."}`, the tool returns a string starting with `"Error:"` rather than crashing.

The rate limiter (`_rate_limit`) is patched in all async tests to prevent artificial delays in CI. Without this patch, tests would be slow and could fail under tight timeout budgets.

## Known Gaps

- No test for authentication — the Reddit API requires OAuth for some endpoints. Whether `RedditClient` supports authenticated requests is not verified.
- No test for pagination — Reddit searches return pages of results; multi-page handling is untested.
- No test for network errors (`aiohttp.ClientError`, timeout) — only API-level error dicts are tested.
- The `time_filter` parameter for trending is accepted but not validated — unknown filter values are not tested.