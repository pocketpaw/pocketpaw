# Tests for UrlExtractTool
# Created: 2026-02-06

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import pocketpaw.tools.builtin.url_extract as url_extract_module
from pocketpaw.tools.builtin.url_extract import UrlExtractTool


@pytest.fixture
def tool():
    return UrlExtractTool()


def _addrinfo(ip: str, family: int = socket.AF_INET) -> list[tuple]:
    return [
        (
            family,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, 443),
        )
    ]


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

        with (
            patch(
                "pocketpaw.tools.builtin.url_extract._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
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

        with (
            patch(
                "pocketpaw.tools.builtin.url_extract._resolve_public_ip",
                AsyncMock(return_value="93.184.216.34"),
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
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

        bad_resp = MagicMock()
        bad_resp.status_code = 404
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )

        async def mock_get(method, url, content=None):
            assert method == "GET"
            assert content is None
            if "good" in url:
                return good_resp
            return bad_resp

        with (
            patch(
                "pocketpaw.tools.builtin.url_extract._resolve_public_ip",
                AsyncMock(side_effect=["93.184.216.34", "93.184.216.35"]),
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):

            async def mock_client_get(url):
                return await mock_get("GET", url, None)

            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=mock_client_get)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(
                urls=["https://good.example.com", "https://bad.example.com"]
            )

        assert "Good" in result
        assert "Error fetching URL" in result

    async def test_ip_pinning_transport_rewrites_host_and_sets_sni(self):
        transport = url_extract_module.IPPinningTransport(
            pinned_ip="93.184.216.34",
            original_host="example.com",
        )
        request = httpx.Request("GET", "https://example.com/path")

        delegated_response = httpx.Response(
            status_code=200,
            request=request,
            text="ok",
        )
        transport._transport.handle_async_request = AsyncMock(return_value=delegated_response)

        response = await transport.handle_async_request(request)

        awaited_call = transport._transport.handle_async_request.await_args
        assert awaited_call is not None
        forwarded_request = awaited_call.args[0]
        assert forwarded_request.url.host == "93.184.216.34"
        assert forwarded_request.headers["host"] == "example.com"
        assert forwarded_request.extensions["sni_hostname"] == "example.com"
        assert response.status_code == 200

        await transport.aclose()

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_local_extract_blocks_private_dns_result(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(url_extract_provider="local")

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Should never be used"
        mock_html2text.HTML2Text.return_value = mock_converter

        loop = MagicMock()
        loop.getaddrinfo = AsyncMock(return_value=_addrinfo("127.0.0.1"))

        with (
            patch("pocketpaw.tools.builtin.url_extract._get_running_loop", return_value=loop),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=AssertionError("network should not be called"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["http://blocked.example.com"])

        assert "Error fetching URL" in result
        assert "blocked" in result.lower()
        assert "127.0.0.1" not in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_local_extract_blocks_ipv4_mapped_ipv6(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(url_extract_provider="local")

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Should never be used"
        mock_html2text.HTML2Text.return_value = mock_converter

        loop = MagicMock()
        loop.getaddrinfo = AsyncMock(return_value=_addrinfo("::ffff:127.0.0.1", socket.AF_INET6))

        with (
            patch("pocketpaw.tools.builtin.url_extract._get_running_loop", return_value=loop),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=AssertionError("network should not be called"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["http://mapped.example.com"])

        assert "Error fetching URL" in result
        assert "blocked" in result.lower()
        assert "127.0.0.1" not in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_local_extract_blocks_private_redirect_hop(self, mock_settings, tool):
        mock_settings.return_value = MagicMock(url_extract_provider="local")

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Should never be used"
        mock_html2text.HTML2Text.return_value = mock_converter

        loop = MagicMock()

        async def mock_getaddrinfo(host, port, *args, **kwargs):
            if host == "public.example.com":
                return _addrinfo("93.184.216.34")
            if host == "internal.example.com":
                return _addrinfo("127.0.0.1")
            raise AssertionError(f"unexpected host lookup: {host}")

        loop.getaddrinfo = AsyncMock(side_effect=mock_getaddrinfo)

        redirect_resp = MagicMock(spec=httpx.Response)
        redirect_resp.status_code = 302
        redirect_resp.headers = {"location": "http://internal.example.com/admin"}
        redirect_resp.text = ""
        redirect_resp.raise_for_status = MagicMock()
        redirect_resp.is_redirect = True

        with (
            patch("pocketpaw.tools.builtin.url_extract._get_running_loop", return_value=loop),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=redirect_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["http://public.example.com/start"])

        assert "Error fetching URL" in result
        assert "blocked" in result.lower()
        assert "127.0.0.1" not in result

    @patch("pocketpaw.tools.builtin.url_extract.get_settings")
    async def test_dns_toctou_resolver_flip_is_blocked(self, mock_settings, tool):
        """Second-hop DNS change to private IP is blocked before fetch."""
        mock_settings.return_value = MagicMock(url_extract_provider="local")

        mock_html2text = MagicMock()
        mock_converter = MagicMock()
        mock_converter.handle.return_value = "Should never be used"
        mock_html2text.HTML2Text.return_value = mock_converter

        first_hop_response = MagicMock(spec=httpx.Response)
        first_hop_response.status_code = 302
        first_hop_response.headers = {"location": "http://flip.example.com/final"}
        first_hop_response.text = ""
        first_hop_response.raise_for_status = MagicMock()
        first_hop_response.is_redirect = True

        with (
            patch(
                "pocketpaw.tools.builtin.url_extract._resolve_public_ip",
                AsyncMock(
                    side_effect=[
                        "93.184.216.34",
                        ValueError("Blocked URL: resolved to non-public IP address."),
                    ]
                ),
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch.dict("sys.modules", {"html2text": mock_html2text}),
        ):
            mock_client = MagicMock()
            mock_client.get = AsyncMock(return_value=first_hop_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(urls=["http://safe.example.com/start"])

        assert "Error fetching URL" in result
        assert "blocked" in result.lower()
        assert "93.184.216.34" not in result
        assert "127.0.0.1" not in result

    async def test_safe_get_pins_ip_and_avoids_second_dns_lookup(self):
        """Validate once, then connect by pinned IP without re-resolving hostname."""

        loop = MagicMock()
        loop.getaddrinfo = AsyncMock(
            side_effect=[
                _addrinfo("93.184.216.34"),
                _addrinfo("127.0.0.1"),
            ]
        )

        delegated_response = httpx.Response(
            status_code=200,
            request=httpx.Request("GET", "http://93.184.216.34/start"),
            text="ok",
        )

        mock_transport = MagicMock()
        mock_transport.handle_async_request = AsyncMock(return_value=delegated_response)
        mock_transport.aclose = AsyncMock()

        with (
            patch("pocketpaw.tools.builtin.url_extract._get_running_loop", return_value=loop),
            patch(
                "pocketpaw.tools.builtin.url_extract.httpx.AsyncHTTPTransport",
                return_value=mock_transport,
            ),
        ):
            response = await url_extract_module._safe_get("http://flip.example.com/start")

        assert response.status_code == 200
        assert loop.getaddrinfo.await_count == 1

        awaited_call = mock_transport.handle_async_request.await_args
        assert awaited_call is not None
        forwarded_request = awaited_call.args[0]
        assert forwarded_request.url.host == "93.184.216.34"
        assert forwarded_request.headers["host"] == "flip.example.com"
        assert forwarded_request.extensions["sni_hostname"] == "flip.example.com"

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
