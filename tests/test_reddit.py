# Tests for Reddit integration (Sprint 29)

from unittest.mock import AsyncMock, patch


class TestRedditToolSchemas:
    """Test Reddit tool properties and schemas."""

    def test_search_tool(self):
        from pocketclaw.tools.builtin.reddit import RedditSearchTool

        tool = RedditSearchTool()
        assert tool.name == "reddit_search"
        assert tool.trust_level == "standard"
        assert "query" in tool.parameters["properties"]
        assert "subreddit" in tool.parameters["properties"]

    def test_read_tool(self):
        from pocketclaw.tools.builtin.reddit import RedditReadTool

        tool = RedditReadTool()
        assert tool.name == "reddit_read"
        assert "url" in tool.parameters["properties"]
        assert "url" in tool.parameters["required"]

    def test_trending_tool(self):
        from pocketclaw.tools.builtin.reddit import RedditTrendingTool

        tool = RedditTrendingTool()
        assert tool.name == "reddit_trending"
        assert "subreddit" in tool.parameters["properties"]
        assert "time_filter" in tool.parameters["properties"]


class TestRedditClientFormatPost:
    """Test RedditClient._format_post helper."""

    def test_format_post(self):
        from pocketclaw.integrations.reddit import RedditClient

        post = {
            "title": "Test Post",
            "author": "testuser",
            "subreddit_name_prefixed": "r/python",
            "score": 42,
            "num_comments": 10,
            "permalink": "/r/python/comments/abc/test_post/",
            "created_utc": 1700000000,
            "is_self": True,
        }
        result = RedditClient._format_post(post)
        assert result["title"] == "Test Post"
        assert result["author"] == "testuser"
        assert result["score"] == 42
        assert "reddit.com" in result["url"]

    def test_format_post_deleted(self):
        from pocketclaw.integrations.reddit import RedditClient

        post = {}
        result = RedditClient._format_post(post)
        assert result["author"] == "[deleted]"
        assert result["title"] == ""


async def test_reddit_search_success():
    from pocketclaw.tools.builtin.reddit import RedditSearchTool

    tool = RedditSearchTool()

    mock_posts = [
        {
            "title": "Best Python Frameworks 2026",
            "author": "coder123",
            "subreddit": "r/python",
            "score": 150,
            "num_comments": 30,
            "url": "https://reddit.com/r/python/comments/abc/best_python/",
        }
    ]

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.search",
        new_callable=AsyncMock,
        return_value=mock_posts,
    ):
        # Also skip rate limiting
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(query="python frameworks", subreddit="python")

    assert "Best Python Frameworks" in result
    assert "150" in result


async def test_reddit_search_no_results():
    from pocketclaw.tools.builtin.reddit import RedditSearchTool

    tool = RedditSearchTool()

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.search",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(query="xyznonexistent123")

    assert "No posts found" in result


async def test_reddit_read_success():
    from pocketclaw.tools.builtin.reddit import RedditReadTool

    tool = RedditReadTool()

    mock_post = {
        "title": "My Post",
        "author": "user1",
        "subreddit": "r/test",
        "score": 100,
        "num_comments": 5,
        "url": "https://reddit.com/r/test/comments/abc/",
        "selftext": "This is the post body.",
        "comments": [
            {"author": "commenter1", "body": "Great post!", "score": 20},
        ],
    }

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.get_post",
        new_callable=AsyncMock,
        return_value=mock_post,
    ):
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(url="https://reddit.com/r/test/comments/abc/")

    assert "My Post" in result
    assert "This is the post body" in result
    assert "Great post!" in result


async def test_reddit_trending_success():
    from pocketclaw.tools.builtin.reddit import RedditTrendingTool

    tool = RedditTrendingTool()

    mock_posts = [
        {
            "title": "Trending Post 1",
            "author": "user1",
            "subreddit": "r/all",
            "score": 5000,
            "num_comments": 200,
            "url": "https://reddit.com/r/all/comments/xyz/",
        }
    ]

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.get_subreddit_top",
        new_callable=AsyncMock,
        return_value=mock_posts,
    ):
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(subreddit="all", time_filter="day")

    assert "Trending Post 1" in result
    assert "5,000" in result


async def test_reddit_trending_empty():
    from pocketclaw.tools.builtin.reddit import RedditTrendingTool

    tool = RedditTrendingTool()

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.get_subreddit_top",
        new_callable=AsyncMock,
        return_value=[],
    ):
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(subreddit="deadsubreddit")

    assert "No trending posts" in result


async def test_reddit_read_error():
    from pocketclaw.tools.builtin.reddit import RedditReadTool

    tool = RedditReadTool()

    mock_post = {"error": "Unexpected response format"}

    with patch(
        "pocketclaw.integrations.reddit.RedditClient.get_post",
        new_callable=AsyncMock,
        return_value=mock_post,
    ):
        with patch("pocketclaw.integrations.reddit._rate_limit", new_callable=AsyncMock):
            result = await tool.execute(url="https://reddit.com/bad")

    assert result.startswith("Error:")
