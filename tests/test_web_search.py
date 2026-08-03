# Tests for Feature 1: WebSearchTool
# Created: 2026-02-06

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketpaw.tools.builtin.web_search import WebSearchTool


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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_missing_tavily_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="tavily",
            tavily_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Tavily API key" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_missing_brave_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="brave",
            brave_search_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Brave Search API key" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_unknown_provider(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(web_search_provider="duckduckgo")
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Unknown search provider" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_parallel_missing_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="parallel",
            parallel_api_key=None,
        )
        result = await tool.execute(query="test")
        assert "Error" in result
        assert "Parallel AI API key" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
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


class TestLiteLLMSearchProvider:
    """The 'litellm' provider — search through the proxy, not a vendor.

    Exists because the obvious-looking route does not work: pydantic-ai's
    native ``WebSearch`` asks the MODEL's provider to search inside
    ``chat/completions``, and the reference gateway answers 200 while
    searching nothing. Its search lives behind ``POST /v1/search``, which only
    a tool can reach.
    """

    @staticmethod
    def _client(mock_resp):
        client = AsyncMock()
        client.post.return_value = mock_resp
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_posts_to_the_proxy_search_endpoint_with_the_configured_tool(
        self, mock_settings, tool
    ):
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="https://gw.example.com/",
            litellm_search_api_base=None,
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="web_search",
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [{"title": "T", "url": "https://u", "snippet": "S"}]}

        with patch("httpx.AsyncClient") as cls:
            client = self._client(resp)
            cls.return_value = client
            result = await tool.execute(query="who won", num_results=3)

        url, kwargs = client.post.call_args[0][0], client.post.call_args[1]
        # Trailing slash on the base must not produce '//v1/search'.
        assert url == "https://gw.example.com/v1/search", url
        assert kwargs["json"]["search_tool_name"] == "web_search"
        assert kwargs["json"]["query"] == "who won"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-proxy"
        assert "T" in result and "https://u" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_snippet_is_normalised_into_the_shared_result_shape(self, mock_settings, tool):
        """The gateway returns ``snippet``; every other provider yields ``content``.

        Without the rename the formatter prints an empty body for every hit —
        results that look present and say nothing.
        """
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="https://gw.example.com",
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="web_search",
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "results": [{"title": "T", "url": "https://u", "snippet": "the useful part"}]
        }

        with patch("httpx.AsyncClient") as cls:
            cls.return_value = self._client(resp)
            result = await tool.execute(query="q")

        assert "the useful part" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_an_unregistered_tool_name_reports_what_to_do(self, mock_settings, tool):
        """The likely misconfiguration, and the proxy names it in the body.

        A bare '500' would send someone reading gateway logs; the registered
        names are one documented GET away.
        """
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="https://gw.example.com",
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="nope",
        )
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {
            "error": {"message": "Search tool 'nope' not found in router.search_tools"}
        }
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
        )

        with patch("httpx.AsyncClient") as cls:
            cls.return_value = self._client(resp)
            result = await tool.execute(query="q")

        assert "not found in router.search_tools" in result
        assert "/v1/search/tools" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_missing_base_url_says_so_instead_of_calling_nothing(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="",
            litellm_search_api_base=None,
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="web_search",
        )
        result = await tool.execute(query="q")
        assert "LITELLM_API_BASE" in result

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_unknown_provider_lists_litellm_as_an_option(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(web_search_provider="bogus")
        assert "litellm" in await tool.execute(query="q")


class TestSearchBaseUrlOverride:
    """Search must survive a proxy being chained in front of the gateway."""

    @staticmethod
    def _client(resp):
        client = AsyncMock()
        client.post.return_value = resp
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    @staticmethod
    def _resp():
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [{"title": "T", "url": "u", "snippet": "s"}]}
        return resp

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_search_can_bypass_a_proxy_that_only_handles_completions(
        self, mock_settings, tool
    ):
        """The Headroom case, and the reason this override exists.

        A compression proxy intercepts /v1/chat/completions, /v1/messages and
        /v1/responses. It knows nothing about /v1/search. So a deployment that
        repoints ``litellm_api_base`` at it keeps completions working and 404s
        every web search — a break visible only in the tool.
        """
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="https://headroom.internal",  # completions detour
            litellm_search_api_base="https://gw.example.com",  # real gateway
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="web_search",
        )

        with patch("httpx.AsyncClient") as cls:
            client = self._client(self._resp())
            cls.return_value = client
            await tool.execute(query="q")

        assert client.post.call_args[0][0] == "https://gw.example.com/v1/search"

    @patch("pocketpaw.tools.builtin.web_search.get_settings")
    async def test_it_falls_back_to_the_shared_base_when_unset(self, mock_settings, tool):
        """The override is opt-in; nothing changes for a normal deployment."""
        mock_settings.return_value = MagicMock(
            web_search_provider="litellm",
            litellm_api_base="https://gw.example.com",
            litellm_search_api_base=None,
            litellm_api_key="sk-proxy",
            litellm_search_tool_name="web_search",
        )

        with patch("httpx.AsyncClient") as cls:
            client = self._client(self._resp())
            cls.return_value = client
            await tool.execute(query="q")

        assert client.post.call_args[0][0] == "https://gw.example.com/v1/search"
