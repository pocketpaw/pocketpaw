---
{
  "title": "Reddit Tools: Search, Read Posts, and Surface Trending Content",
  "summary": "PocketPaw ships three Reddit tools — `reddit_search`, `reddit_read`, and `reddit_trending` — that give agents access to Reddit content without requiring an API key. They delegate to a shared `RedditClient` integration and return richly formatted markdown text suitable for direct LLM consumption.",
  "concepts": [
    "RedditSearchTool",
    "RedditReadTool",
    "RedditTrendingTool",
    "BaseTool",
    "RedditClient",
    "trust_level",
    "subreddit",
    "sort_order",
    "time_filter",
    "media_integrations"
  ],
  "categories": [
    "tools",
    "media-integrations",
    "social-media",
    "content-retrieval"
  ],
  "source_docs": [
    "127afe097b3852ab"
  ],
  "backlinks": null,
  "word_count": 588,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `reddit.py` module is part of PocketPaw's Phase 4 Media Integrations and exposes Reddit as a first-class data source for agents. Three `BaseTool` subclasses cover the primary Reddit use-cases: keyword search, reading a full post with comments, and fetching top posts from any subreddit. All three tools operate at `standard` trust level, meaning they require no elevated user permission to invoke.

## Why No API Key?

The tools deliberately advertise "No API key required" in their descriptions. This design choice uses Reddit's public JSON API endpoints (the `/.json` suffix pattern or the legacy OAuth-free paths) rather than the official Reddit API. The tradeoff is rate-limit fragility — Reddit throttles unauthenticated requests more aggressively — but it dramatically lowers the barrier for new users who would otherwise have to register an application in Reddit's developer portal just to read posts.

## RedditSearchTool

```python
class RedditSearchTool(BaseTool):
    async def execute(self, query, subreddit=None, sort="relevance", limit=10) -> str:
        from pocketpaw.integrations.reddit import RedditClient
        client = RedditClient()
        posts = await client.search(query, subreddit=subreddit, sort=sort, limit=limit)
        ...
```

`reddit_search` accepts a free-text query and an optional `subreddit` filter. The `sort` parameter maps to Reddit's five sort orders (`relevance`, `hot`, `top`, `new`, `comments`). Results are capped at 25 — a pragmatic limit that keeps output within LLM context windows while still giving enough signal for most tasks.

The empty-result branch generates a contextual message that includes the subreddit name when one was requested, preventing ambiguous "nothing found" responses where the user can't tell if the subreddit was wrong or the query was too narrow.

## RedditReadTool

```python
class RedditReadTool(BaseTool):
    async def execute(self, url: str) -> str:
        client = RedditClient()
        post = await client.get_post(url)
        if "error" in post:
            return self._error(post["error"])
        ...
```

`reddit_read` accepts either a full Reddit URL or a bare post ID, making it forgiving of how users naturally reference Reddit posts. The response includes the post body (`selftext`) when it exists, then lists the top N comments truncated to 300 characters each. The 300-character comment truncation prevents a single viral thread from flooding the context window.

The `error` key check in the returned dict is an explicit contract with `RedditClient` — if the integration layer encounters a 404 or a deleted post, it returns `{"error": "..."}` rather than raising an exception, so `RedditReadTool` can surface a clean user-facing message instead of a raw traceback.

## RedditTrendingTool

```python
class RedditTrendingTool(BaseTool):
    async def execute(self, subreddit="all", time_filter="day", limit=10) -> str:
        posts = await client.get_subreddit_top(subreddit=subreddit, time_filter=time_filter, limit=limit)
        ...
```

`reddit_trending` defaults to `r/all` (the Reddit frontpage) with a `day` time filter, making it immediately useful with no arguments. The `time_filter` parameter supports Reddit's six horizons: `hour`, `day`, `week`, `month`, `year`, `all`. All parameters are optional — `required: []` in the schema — so the agent can invoke it conversationally ("what's trending on Reddit?") without needing to specify anything.

## Error Handling Pattern

All three tools wrap their `execute` bodies in a broad `except Exception` block that calls `self._error()`, inherited from `BaseTool`. This prevents any integration-layer exception (network timeout, JSON decode error, rate-limit HTTP 429) from propagating as an unhandled exception into the agent loop. The agent sees a formatted error string and can decide whether to retry or inform the user.

## Known Gaps

- The `RedditClient` integration is not shown in this file; its resilience (retry logic, rate-limit backoff) is opaque from the tool layer.
- Comment pagination is not supported — only the top comments included in the initial API response are shown.
- Authentication (OAuth) for accessing user-specific content (saved posts, upvotes) is not implemented.
