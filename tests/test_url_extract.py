# Tests for UrlExtractTool
# Created: 2026-02-06

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketclaw.tools.builtin.url_extract import UrlExtractTool


@pytest.fixture
def tool():
    return UrlExtractTool()


class TestUrlExtractTool:
    """Tests for UrlExtractTool."""

    def test_name(self, tool):
        assert tool.name == "url_extract"

    def test_trust_level(self, tool):
        assert tool.trust_level == "standard"

    def test_parameters_schema(self, tool):
        params = tool.parameters
        assert "urls" in params["properties"]
        assert params["properties"]["urls"]["type"] == "array"
        assert "urls" in params["required"]

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_parallel_extract_success(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="parallel",
            parallel_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Example Page",
                    "full_content": "This is the page content.",
                }
            ],
            "errors": [],
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Example Page" in result
        assert "This is the page content." in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_parallel_extract_multiple_urls(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="parallel",
            parallel_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "Page A",
                    "full_content": "Content A",
                },
                {
                    "url": "https://example.com/b",
                    "title": "Page B",
                    "full_content": "Content B",
                },
            ],
            "errors": [],
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com/a", "https://example.com/b"])

        # Multiple URLs use numbered list format
        assert "Page A" in result
        assert "Page B" in result
        assert "2 URLs" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_parallel_missing_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="parallel",
            parallel_api_key=None,
        )
        result = await tool.execute(urls=["https://example.com"])
        assert "Error" in result
        assert "Parallel AI API key" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_parallel_http_error(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="parallel",
            parallel_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Error" in result
        assert "500" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_auto_mode_with_key(self, mock_settings, tool):
        """Auto mode routes to parallel when API key is set."""
        mock_settings.return_value = MagicMock(
            url_extract_provider="auto",
            parallel_api_key="test-key",
        )

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://example.com",
                    "title": "Auto Test",
                    "full_content": "Auto content",
                }
            ],
            "errors": [],
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Auto Test" in result
        # Verify it called post (Parallel), not get (local)
        mock_client.post.assert_called_once()

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_auto_mode_without_key(self, mock_settings, tool):
        """Auto mode routes to local when no API key is set."""
        mock_settings.return_value = MagicMock(
            url_extract_provider="auto",
            parallel_api_key=None,
        )

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Converted content"
        mock_html2text.HTML2Text.return_value = mock_converter

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        mock_resp.text = "<html><title>Local Test</title><body>Hello</body></html>"

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Local Test" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_local_extract_success(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="local",
        )

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "# Hello World\n\nThis is content."
        mock_html2text.HTML2Text.return_value = mock_converter

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.text = "<html><title>Hello World</title><body><h1>Hello</h1></body></html>"

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Hello World" in result
        assert "This is content." in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_local_missing_html2text(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="local",
        )

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "html2text":
                raise ImportError("No module named 'html2text'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            result = await tool.execute(urls=["https://example.com"])

        assert "Error" in result
        assert "html2text" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_local_http_error_per_url(self, mock_settings, tool):
        """One URL fails, others succeed."""
        mock_settings.return_value = MagicMock(
            url_extract_provider="local",
        )

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Good content"
        mock_html2text.HTML2Text.return_value = mock_converter

        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.raise_for_status = MagicMock()
        good_resp.headers = {"content-type": "text/html"}
        good_resp.text = "<html><title>Good</title><body>OK</body></html>"

        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )

        async def mock_get(url):
            if "good" in url:
                return good_resp
            return bad_resp

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(
                urls=["https://good.example.com", "https://bad.example.com"]
            )

        assert "Good" in result
        assert "Error fetching URL" in result

    @patch("pocketclaw.tools.builtin.url_extract.get_settings")
    async def test_unknown_provider(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(url_extract_provider="unknown")
        result = await tool.execute(urls=["https://example.com"])
        assert "Error" in result
        assert "Unknown extract provider" in result

    async def test_empty_urls(self, tool):
        result = await tool.execute(urls=[])
        assert "Error" in result
        assert "No URLs" in result
