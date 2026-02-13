# Tests for Feature 1: WebSearchTool
# Created: 2026-02-06

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketclaw.tools.builtin.web_search import WebSearchTool


@pytest.fixture
def tool():
    return WebSearchTool()


class TestWebSearchTool:
    """Tests for WebSearchTool."""

    def test_name(self, tool):
        assert tool.name == "web_search"

    def test_trust_level(self, tool):
        assert tool.trust_level == "standard"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert "query" in params["properties"]
        assert "num_results" in params["properties"]
        assert "query" in params["required"]

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_tavily_search_success(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Python Docs",
                    "url": "https://docs.python.org",
                    "content": "Official Python documentation",
                }
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="python docs")

        assert "Python Docs" in result
        assert "https://docs.python.org" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_brave_search_success(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="brave",
            brave_search_api_key="test-brave-key",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Brave Search",
                        "url": "https://brave.com",
                        "description": "Privacy search engine",
                    }
                ]
            }
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="brave search")

        assert "Brave Search" in result
        assert "https://brave.com" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_missing_tavily_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Tavily API key" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_missing_brave_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="brave",
            brave_search_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Brave Search API key" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_unknown_provider(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(web_search_provider="duckduckgo")
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Unknown search provider" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_no_results(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="xyznonexistent")

        assert "No results found" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_http_error(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=MagicMock(),
            response=MagicMock(status_code=401),
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="test")

        assert "Error" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_parallel_search_success(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="parallel",
            parallel_api_key="test-parallel-key",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Parallel AI Docs",
                    "url": "https://docs.parallel.ai",
                    "excerpts": ["First excerpt.", "Second excerpt."],
                }
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="parallel ai")

        assert "Parallel AI Docs" in result
        assert "https://docs.parallel.ai" in result
        # Verify headers were sent correctly
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"]["x-api-key"] == "test-parallel-key"
        assert "parallel-beta" in call_kwargs.kwargs["headers"]

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_parallel_missing_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="parallel",
            parallel_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Parallel AI API key" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_parallel_no_results(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="parallel",
            parallel_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(query="nothing here")

        assert "No results found" in result

    @patch("pocketclaw.tools.builtin.web_search.get_settings")
    async def test_num_results_clamped(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [{"title": "A", "url": "u", "content": "c"}]}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # num_results=50 should be clamped to 10
            result = await tool.execute(query="test", num_results=50)

        assert "A" in result
