# Reddit Client — read-only access via Reddit JSON API.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations
#
# Uses Reddit's public JSON API (append .json to URLs).
# No API key or authentication required for read-only access.

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_REDDIT_BASE = "https://www.reddit.com"
_USER_AGENT = "PocketPaw/1.0 (AI Assistant; +https://github.com/pocketpaw/pocketpaw)"

# Rate limit: Reddit requires ~1 req/sec for unauthenticated access
_last_request_time: float = 0


async def _rate_limit():
    """Ensure we don't exceed Reddit's rate limit."""
    global _last_request_time
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_request_time
    if elapsed < 1.0:
        await asyncio.sleep(1.0 - elapsed)
    _last_request_time = asyncio.get_event_loop().time()


class RedditClient:
    """Read-only client for Reddit using the public JSON API.

    No API key required. Respects Reddit's rate limit (1 req/sec).
    """

    async def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search Reddit posts.

        Args:
            query: Search query.
            subreddit: Subreddit to search within (optional).
            sort: Sort order — 'relevance', 'hot', 'top', 'new', 'comments'.
            limit: Maximum results.

        Returns:
            List of post dicts.
        """
        await _rate_limit()

        if subreddit:
            url = f"{_REDDIT_BASE}/r/{subreddit}/search.json"
            params = {"q": query, "sort": sort, "limit": min(limit, 25), "restrict_sr": "on"}
        else:
            url = f"{_REDDIT_BASE}/search.json"
            params = {"q": query, "sort": sort, "limit": min(limit, 25)}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()

        posts = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append(self._format_post(post))

        return posts

    async def get_post(self, url: str) -> dict[str, Any]:
        """Read a Reddit post with top comments.

        Args:
            url: Full Reddit post URL or post ID.

        Returns:
            Dict with post data and top comments.
        """
        await _rate_limit()

        # Handle both full URLs and post IDs
        if url.startswith("http"):
            json_url = url.rstrip("/") + ".json"
        else:
            json_url = f"{_REDDIT_BASE}/comments/{url}.json"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                json_url,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()

        # Reddit returns [post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 2:
            return {"error": "Unexpected response format"}

        post_data = data[0]["data"]["children"][0]["data"]
        post = self._format_post(post_data)
        post["selftext"] = post_data.get("selftext", "")[:3000]

        # Extract top comments
        comments = []
        for child in data[1].get("data", {}).get("children", [])[:10]:
            if child.get("kind") != "t1":
                continue
            c = child.get("data", {})
            comments.append(
                {
                    "author": c.get("author", "[deleted]"),
                    "body": c.get("body", "")[:500],
                    "score": c.get("score", 0),
                }
            )
        post["comments"] = comments

        return post

    async def get_subreddit_top(
        self,
        subreddit: str = "all",
        time_filter: str = "day",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get top/trending posts from a subreddit.

        Args:
            subreddit: Subreddit name (default 'all' for frontpage).
            time_filter: Time range — 'hour', 'day', 'week', 'month', 'year', 'all'.
            limit: Maximum results.

        Returns:
            List of post dicts.
        """
        await _rate_limit()

        url = f"{_REDDIT_BASE}/r/{subreddit}/top.json"
        params = {"t": time_filter, "limit": min(limit, 25)}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params=params,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()

        posts = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append(self._format_post(post))

        return posts

    @staticmethod
    def _format_post(post: dict) -> dict[str, Any]:
        """Format a Reddit post into a clean dict."""
        return {
            "title": post.get("title", ""),
            "author": post.get("author", "[deleted]"),
            "subreddit": post.get("subreddit_name_prefixed", ""),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "url": f"https://reddit.com{post.get('permalink', '')}",
            "created_utc": post.get("created_utc", 0),
            "is_self": post.get("is_self", False),
        }
