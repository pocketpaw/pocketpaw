# Reddit tools — search, read posts, trending.
# Created: 2026-02-09
# Part of Phase 4 Media Integrations

import logging
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class RedditSearchTool(BaseTool):
    """Search Reddit for posts."""

    @property
    def name(self) -> str:
        return "reddit_search"

    @property
    def description(self) -> str:
        return (
            "Search Reddit for posts. Can search all of Reddit or within a specific subreddit. "
            "No API key required."
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
                    "description": "Search query",
                },
                "subreddit": {
                    "type": "string",
                    "description": "Subreddit to search within (optional, e.g. 'python')",
                },
                "sort": {
                    "type": "string",
                    "description": "Sort: relevance, hot, top, new, comments (default: relevance)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 25)",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        limit: int = 10,
    ) -> str:
        try:
            from pocketclaw.integrations.reddit import RedditClient

            client = RedditClient()
            posts = await client.search(query, subreddit=subreddit, sort=sort, limit=limit)

            if not posts:
                sub_str = f" in r/{subreddit}" if subreddit else ""
                return f"No posts found for '{query}'{sub_str}."

            sub_str = f" in r/{subreddit}" if subreddit else ""
            lines = [f"Reddit search results for '{query}'{sub_str}:\n"]
            for i, p in enumerate(posts, 1):
                lines.append(
                    f"{i}. **{p['title']}** ({p['subreddit']})\n"
                    f"   Score: {p['score']:,} | Comments: {p['num_comments']:,} | "
                    f"by u/{p['author']}\n"
                    f"   {p['url']}"
                )
            return "\n".join(lines)

        except Exception as e:
            return self._error(f"Reddit search failed: {e}")


class RedditReadTool(BaseTool):
    """Read a Reddit post with top comments."""

    @property
    def name(self) -> str:
        return "reddit_read"

    @property
    def description(self) -> str:
        return "Read a Reddit post and its top comments. Accepts a full Reddit URL or a post ID."

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full Reddit post URL or post ID",
                },
            },
            "required": ["url"],
        }

    async def execute(self, url: str) -> str:
        try:
            from pocketclaw.integrations.reddit import RedditClient

            client = RedditClient()
            post = await client.get_post(url)

            if "error" in post:
                return self._error(post["error"])

            lines = [
                f"**{post['title']}** ({post.get('subreddit', '')})",
                f"by u/{post.get('author', '[deleted]')} | "
                f"Score: {post.get('score', 0):,} | "
                f"Comments: {post.get('num_comments', 0):,}",
            ]

            selftext = post.get("selftext", "")
            if selftext:
                lines.append(f"\n{selftext}")

            comments = post.get("comments", [])
            if comments:
                lines.append(f"\n**Top {len(comments)} comments:**\n")
                for c in comments:
                    score = c.get("score", 0)
                    body = c.get("body", "")[:300]
                    lines.append(f"- **u/{c.get('author', '[deleted]')}** ({score:+d}): {body}")

            return "\n".join(lines)

        except Exception as e:
            return self._error(f"Reddit read failed: {e}")


class RedditTrendingTool(BaseTool):
    """Get trending/top posts from a subreddit."""

    @property
    def name(self) -> str:
        return "reddit_trending"

    @property
    def description(self) -> str:
        return (
            "Get top/trending posts from a subreddit. "
            "Defaults to r/all (frontpage). No API key required."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "subreddit": {
                    "type": "string",
                    "description": "Subreddit name (default: 'all' for frontpage)",
                },
                "time_filter": {
                    "type": "string",
                    "description": "Time range: hour, day, week, month, year, all (default: day)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 25)",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        subreddit: str = "all",
        time_filter: str = "day",
        limit: int = 10,
    ) -> str:
        try:
            from pocketclaw.integrations.reddit import RedditClient

            client = RedditClient()
            posts = await client.get_subreddit_top(
                subreddit=subreddit, time_filter=time_filter, limit=limit
            )

            if not posts:
                return f"No trending posts found in r/{subreddit}."

            lines = [f"Top posts in r/{subreddit} ({time_filter}):\n"]
            for i, p in enumerate(posts, 1):
                lines.append(
                    f"{i}. **{p['title']}**\n"
                    f"   Score: {p['score']:,} | Comments: {p['num_comments']:,} | "
                    f"by u/{p['author']}\n"
                    f"   {p['url']}"
                )
            return "\n".join(lines)

        except Exception as e:
            return self._error(f"Reddit trending failed: {e}")
