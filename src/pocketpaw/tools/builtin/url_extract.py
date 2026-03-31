# URL Extract tool — fetch clean content from URLs via Parallel AI or local fallback.
# Created: 2026-02-06

import asyncio
import ipaddress
import logging
import socket
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_PARALLEL_EXTRACT_URL = "https://api.parallel.ai/v1beta/extract"
_MAX_CONTENT_CHARS = 50_000
_DEFAULT_HTTP_TIMEOUT = 30
_MAX_REDIRECT_HOPS = 5
_ALLOWED_SCHEMES = {"http", "https"}


class IPPinningTransport(httpx.AsyncBaseTransport):
    """Transport that connects to a pre-resolved IP while preserving Host/SNI."""

    def __init__(
        self,
        pinned_ip: str,
        original_host: str,
        host_header: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._pinned_ip = pinned_ip
        self._original_host = original_host
        self._host_header = host_header or original_host
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(host=self._pinned_ip)
        request.headers = request.headers.copy()
        request.headers["host"] = self._host_header
        request.extensions = {**request.extensions, "sni_hostname": self._original_host}
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _get_running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def _normalize_ip_address(raw_ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    parsed = ipaddress.ip_address(raw_ip)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _ensure_supported_url(url: httpx.URL) -> None:
    if url.scheme not in _ALLOWED_SCHEMES:
        raise ValueError("Blocked URL: only http and https URLs are allowed.")
    if not url.host:
        raise ValueError("Blocked URL: hostname is required.")


def _port_for_url(url: httpx.URL) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def _validate_public_ip(raw_ip: str) -> str:
    parsed_ip = _normalize_ip_address(raw_ip)
    if not parsed_ip.is_global:
        raise ValueError("Blocked URL: resolved to non-public IP address.")

    return str(parsed_ip)


async def _resolve_public_ip(hostname: str, port: int) -> str:
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        return _validate_public_ip(hostname)

    loop = _get_running_loop()
    try:
        addrinfo = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Could not resolve URL hostname.") from exc

    if not addrinfo:
        raise ValueError("Could not resolve URL hostname.")

    candidate_ips: list[str] = []
    for record in addrinfo:
        sockaddr = record[4]
        if not sockaddr:
            continue
        raw_ip = sockaddr[0]
        candidate_ips.append(_validate_public_ip(raw_ip))

    if not candidate_ips:
        raise ValueError("Could not resolve URL hostname.")

    return candidate_ips[0]


def _next_redirect_url(current_url: httpx.URL, location: str) -> httpx.URL:
    next_url = current_url.join(location)
    _ensure_supported_url(next_url)
    return next_url


async def _safe_get(
    url: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> httpx.Response:
    current_url = httpx.URL(url)
    _ensure_supported_url(current_url)

    for _ in range(_MAX_REDIRECT_HOPS + 1):
        if current_url.host is None:
            raise ValueError("Blocked URL: hostname is required.")

        pinned_ip = await _resolve_public_ip(current_url.host, _port_for_url(current_url))

        transport = IPPinningTransport(
            pinned_ip=pinned_ip,
            original_host=current_url.host,
        )

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = await client.get(str(current_url))

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location:
                return response

            current_url = _next_redirect_url(current_url, location)
            continue

        return response

    raise ValueError("Blocked URL: too many redirects.")


class UrlExtractTool(BaseTool):
    """Extract clean text content from one or more URLs."""

    @property
    def name(self) -> str:
        return "url_extract"

    @property
    def description(self) -> str:
        return (
            "Fetch and extract clean text content from one or more URLs. "
            "Useful for reading web pages, articles, documentation, or any "
            "publicly accessible URL. Returns markdown-formatted content."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs to extract content from",
                },
            },
            "required": ["urls"],
        }

    async def execute(self, urls: list[str]) -> str:
        """Extract content from the given URLs."""
        if not urls:
            return self._error("No URLs provided.")

        for raw_url in urls:
            try:
                _ensure_supported_url(httpx.URL(raw_url))
            except ValueError as exc:
                return self._error(str(exc))

        settings = get_settings()
        provider = settings.url_extract_provider

        if provider == "auto":
            if settings.parallel_api_key:
                provider = "parallel"
            else:
                provider = "local"

        if provider == "parallel":
            return await self._extract_parallel(urls, settings.parallel_api_key)
        elif provider == "local":
            return await self._extract_local(urls)
        else:
            return self._error(
                f"Unknown extract provider '{provider}'. Use 'auto', 'parallel', or 'local'."
            )

    async def _extract_parallel(self, urls: list[str], api_key: str | None) -> str:
        if not api_key:
            return self._error(
                "Parallel AI API key not configured. "
                "Set POCKETPAW_PARALLEL_API_KEY or switch to 'local' provider."
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    _PARALLEL_EXTRACT_URL,
                    headers={
                        "x-api-key": api_key,
                        "parallel-beta": "search-extract-2025-10-10",
                        "Content-Type": "application/json",
                    },
                    json={
                        "urls": urls,
                        "full_content": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            errors = data.get("errors", [])

            if not results and errors:
                error_msgs = "; ".join(
                    f"{e.get('url', '?')}: {e.get('error', 'unknown')}" for e in errors
                )
                return self._error(f"Extraction failed: {error_msgs}")

            if not results:
                return self._error("No content extracted from the provided URLs.")

            return self._format_results(results, urls)

        except httpx.HTTPStatusError as e:
            return self._error(f"Parallel AI API error: {e.response.status_code}")
        except Exception as e:
            return self._error(f"Extraction failed: {e}")

    async def _extract_local(self, urls: list[str]) -> str:
        try:
            import html2text
        except ImportError:
            return self._error(
                "html2text not installed. Install with: pip install 'pocketpaw[extract]'"
            )

        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0

        results = []
        for url in urls:
            try:
                resp = await _safe_get(url)
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    text = converter.handle(resp.text)
                else:
                    text = resp.text

                results.append(
                    {
                        "url": url,
                        "title": _extract_title(resp.text) if "text/html" in content_type else url,
                        "full_content": text[:_MAX_CONTENT_CHARS],
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "url": url,
                        "title": url,
                        "full_content": f"Error fetching URL: {e}",
                    }
                )

        if not results:
            return self._error("No content extracted from the provided URLs.")

        return self._format_results(results, urls)

    @staticmethod
    def _format_results(results: list[dict], urls: list[str]) -> str:
        if len(urls) == 1:
            r = results[0]
            title = r.get("title", "Untitled")
            content = r.get("full_content", "")[:_MAX_CONTENT_CHARS]
            return f"# {title}\n\n{content}"

        # Multiple URLs: numbered list with previews
        lines = [f"Extracted content from {len(results)} URLs:\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            content = r.get("full_content", "")[:2000]
            lines.append(f"## {i}. {title}\n{url}\n\n{content}\n")
        return "\n".join(lines)


def _extract_title(html: str) -> str:
    """Extract <title> from HTML, falling back to 'Untitled'."""
    import re

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return "Untitled"
