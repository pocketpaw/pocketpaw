---
{
  "title": "Reddit Integration: Read-Only Client via Public JSON API",
  "summary": "PocketPaw's Reddit client provides read-only access to Reddit posts and subreddits using Reddit's public JSON API, requiring no API key or authentication. It enforces a one-request-per-second rate limit to comply with Reddit's unauthenticated access policy.",
  "concepts": [
    "RedditClient",
    "rate limiting",
    "public JSON API",
    "httpx",
    "subreddit search",
    "user-agent policy",
    "read-only access",
    "Phase 4 Media Integrations",
    "unauthenticated API"
  ],
  "categories": [
    "integrations",
    "media",
    "HTTP clients"
  ],
  "source_docs": [
    "763129662aa1bcc4"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `RedditClient` in `src/pocketpaw/integrations/reddit.py` enables PocketPaw agents to query Reddit for posts, trending content, and subreddit activity without any developer account or OAuth flow. This is part of Phase 4 Media Integrations, added in February 2026.

## Why a Custom Client Instead of PRAW?

Reddit's official Python library (PRAW) requires app registration and OAuth credentials. For PocketPaw's use case — read-only content discovery — that overhead is unnecessary. Reddit exposes a fully public JSON API: appending `.json` to any Reddit URL returns structured data. The client exploits this to avoid credential management entirely.

## Rate Limiting: Why It Exists

Reddit's unauthenticated API allows roughly one request per second. Exceeding this results in HTTP 429 errors or temporary IP bans. The module-level `_rate_limit()` coroutine enforces this ceiling using a global `_last_request_time` timestamp. Before every network call, it checks elapsed time and sleeps if less than one second has passed. This prevents callers from accidentally hammering Reddit during rapid agent loops.

The rate limiter is intentionally simple — a single global float — because the client is designed for single-process use. Concurrent multi-process use would bypass this guard.

## RedditClient API

The client exposes two primary operations:

```python
client = RedditClient()

# Search across all of Reddit or within a subreddit
posts = await client.search(
    query="AI agents",
    subreddit="MachineLearning",  # optional
    sort="relevance",             # hot | top | new | comments
    limit=10,
)

# Fetch top posts from a subreddit
top_posts = await client.get_top_posts(subreddit="LocalLLaMA", limit=5)
```

Results are plain Python dicts parsed from Reddit's JSON response, preserving the full post structure (title, score, URL, selftext, etc.).

## User-Agent Policy

Reddit requires a descriptive `User-Agent` header. The client sets:

```
PocketPaw/1.0 (AI Assistant; +https://github.com/pocketpaw/pocketpaw)
```

Sending a generic `python-httpx` string can result in API blocks. The hardcoded value ensures compliance.

## Error Handling

The client uses `httpx` and propagates HTTP errors directly. There is no retry logic — a failed request raises immediately. This keeps the client simple and lets the calling agent decide whether to retry.

## Known Gaps

- **No authentication path**: OAuth-authenticated requests would raise the rate limit to 60 req/min and unlock user-specific data. This is not implemented.
- **Global rate limiter not thread-safe**: The `_last_request_time` global is not protected by a lock. Concurrent async coroutines could still burst past the 1 req/sec limit.
- **No pagination**: Results are capped at Reddit's 25-item maximum per request. There is no cursor or `after` parameter support for deeper pagination.