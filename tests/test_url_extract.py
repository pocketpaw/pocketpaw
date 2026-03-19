# Tests for UrlExtractTool
# Created: 2026-02-06

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pocketpaw.tools.builtin.url_extract import (
    UrlExtractTool,
    _is_private_ip,
    _validate_url,
)


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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_parallel_missing_api_key(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(
            url_extract_provider="parallel",
            parallel_api_key=None,
        )
        result = await tool.execute(urls=["https://example.com"])
        assert "Error" in result
        assert "Parallel AI API key" in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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
        mock_resp.is_redirect = False

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            # Mock DNS resolution
            loop = AsyncMock()
            mock_loop.return_value = loop
            loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]
            )
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Local Test" in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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
        mock_resp.is_redirect = False

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            # Mock DNS resolution
            loop = AsyncMock()
            mock_loop.return_value = loop
            loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]
            )
            
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["https://example.com"])

        assert "Hello World" in result
        assert "This is content." in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
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
        good_resp.is_redirect = False

        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )

        async def mock_get(url, **kwargs):
            # Check the Host header to determine which response to return
            headers = kwargs.get("headers", {})
            host = headers.get("Host", "")
            if "good" in host:
                return good_resp
            return bad_resp

        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
            patch("asyncio.get_running_loop") as mock_loop,
        ):
            # Mock DNS resolution for both URLs
            loop = AsyncMock()
            mock_loop.return_value = loop
            
            async def mock_getaddrinfo(host, port, **kwargs):
                if "good" in host:
                    return [(2, 1, 6, "", ("93.184.216.34", port))]
                else:
                    return [(2, 1, 6, "", ("93.184.216.35", port))]
            
            loop.getaddrinfo = AsyncMock(side_effect=mock_getaddrinfo)
            
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

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_unknown_provider(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(url_extract_provider="unknown")
        result = await tool.execute(urls=["https://example.com"])
        assert "Error" in result
        assert "Unknown extract provider" in result

    async def test_empty_urls(self, tool):
        result = await tool.execute(urls=[])
        assert "Error" in result
        assert "No URLs" in result


class TestSSRFHardening:
    """Tests for SSRF protection in URL extraction."""

    def test_is_private_ip_loopback_ipv4(self):
        assert _is_private_ip("127.0.0.1") is True
        assert _is_private_ip("127.0.0.2") is True

    def test_is_private_ip_loopback_ipv6(self):
        assert _is_private_ip("::1") is True

    def test_is_private_ip_ipv4_mapped_ipv6(self):
        assert _is_private_ip("::ffff:127.0.0.1") is True
        assert _is_private_ip("::ffff:192.168.1.1") is True

    def test_is_private_ip_rfc1918(self):
        assert _is_private_ip("10.0.0.1") is True
        assert _is_private_ip("172.16.0.1") is True
        assert _is_private_ip("192.168.0.1") is True

    def test_is_private_ip_link_local(self):
        assert _is_private_ip("169.254.0.1") is True
        assert _is_private_ip("169.254.169.254") is True  # AWS metadata

    def test_is_private_ip_multicast(self):
        assert _is_private_ip("224.0.0.1") is True

    def test_is_private_ip_public(self):
        assert _is_private_ip("8.8.8.8") is False
        assert _is_private_ip("1.1.1.1") is False

    def test_is_private_ip_invalid(self):
        assert _is_private_ip("not-an-ip") is True

    @pytest.mark.asyncio
    async def test_validate_url_http_scheme(self):
        await _validate_url("http://example.com")

    @pytest.mark.asyncio
    async def test_validate_url_https_scheme(self):
        await _validate_url("https://example.com")

    @pytest.mark.asyncio
    async def test_validate_url_disallowed_scheme(self):
        with pytest.raises(ValueError, match="not allowed"):
            await _validate_url("ftp://example.com")

    @pytest.mark.asyncio
    async def test_validate_url_loopback_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            await _validate_url("http://127.0.0.1")

    @pytest.mark.asyncio
    async def test_validate_url_private_blocked(self):
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = AsyncMock()
            mock_loop.return_value = loop
            loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("192.168.1.1", 80))]
            )
            with pytest.raises(ValueError, match="blocked"):
                await _validate_url("http://private.local")

    @pytest.mark.asyncio
    async def test_validate_url_unresolvable(self):
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = AsyncMock()
            mock_loop.return_value = loop
            loop.getaddrinfo = AsyncMock(
                side_effect=socket.gaierror(11001, "getaddrinfo failed")
            )
            with pytest.raises(ValueError, match="cannot be resolved"):
                await _validate_url("http://this-domain-does-not-exist-12345.test")

    @pytest.mark.asyncio
    async def test_validate_url_no_hostname(self):
        with pytest.raises(ValueError, match="no hostname"):
            await _validate_url("http://")

    @pytest.mark.asyncio
    async def test_safe_get_too_many_redirects(self):
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = AsyncMock()
            mock_loop.return_value = loop
            loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]
            )

            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.is_redirect = True
            mock_resp.headers = {"location": "http://example.com/next"}
            mock_client.get = AsyncMock(return_value=mock_resp)

            with pytest.raises(ValueError, match="Too many redirects"):
                await UrlExtractTool._safe_get(mock_client, "http://example.com/1")

    @pytest.mark.asyncio
    async def test_safe_get_uses_resolved_ip_toctou_fix(self):
        """Verify DNS TOCTOU fix: uses pre-resolved IP, not re-resolving."""
        with patch("asyncio.get_running_loop") as mock_loop:
            loop = AsyncMock()
            mock_loop.return_value = loop
            # Return a safe public IP on DNS resolution
            loop.getaddrinfo = AsyncMock(
                return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]
            )

            mock_client = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.is_redirect = False
            mock_resp.raise_for_status = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)

            await UrlExtractTool._safe_get(mock_client, "http://example.com/page")

            # Verify client.get was called with the resolved IP, not the hostname
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            
            # The first argument should be the URL with the resolved IP
            url_used = call_args[0][0]
            assert "93.184.216.34" in url_used, f"Expected resolved IP in request URL, got: {url_used}"
            
            # Verify Host header is set to the original hostname
            headers = call_args[1].get("headers", {})
            assert headers.get("Host") == "example.com", f"Expected Host header set to hostname, got: {headers}"
            
            # Verify DNS was only queried once (not re-queried on request)
            loop.getaddrinfo.assert_called_once()

