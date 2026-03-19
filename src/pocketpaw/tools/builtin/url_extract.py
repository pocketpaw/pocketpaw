# URL Extract tool — fetch clean content from URLs via Parallel AI or local fallback.
# Created: 2026-02-06

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


def _is_private_ip(addr: str) -> bool:
    """Return True if *addr* resolves to a private/reserved IP address.
    
    Handles IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1).
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True  # unparseable → block
    
    # Handle IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _validate_url(url: str) -> tuple[str, int]:
    """Validate a URL against SSRF attacks and return resolved IP and port.

    Returns the resolved IP address and port for safe use in subsequent requests.
    This prevents DNS TOCTOU attacks by ensuring the validated IP is used for the request.
    
    Raises ValueError if the URL targets a private/reserved IP, uses a
    disallowed scheme, or cannot be resolved.
    
    Uses non-blocking DNS resolution to avoid blocking the event loop.
    Does not expose internal IP details to error messages (logged instead).
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"URL scheme '{parsed.scheme}' not allowed (only http/https)"
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Resolve hostname to IPs and check every result (prevents DNS rebinding
    # where one A-record is public and another is private).
    # Use non-blocking DNS resolution to not block the event loop.
    try:
        loop = asyncio.get_running_loop()
        addrinfos = await loop.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        logger.warning(f"DNS resolution failed for {hostname}: {exc}")
        raise ValueError("URL cannot be resolved") from exc

    # Validate all resolved IPs and return the first safe one.
    # If multiple IPs are returned (e.g., A and AAAA records), all must be safe.
    resolved_ips = []
    for family, _type, _proto, _canonname, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        resolved_ip = sockaddr[0]
        resolved_ips.append(ip_str)
        if _is_private_ip(ip_str):
            logger.warning(
                f"SSRF blocked: URL {url} resolves to private/reserved address {ip_str}"
            )
            raise ValueError("URL resolves to a blocked address")
    
    # Return the first resolved IP to use in the actual request
    return (resolved_ips[0], port)

_PARALLEL_EXTRACT_URL = "https://api.parallel.ai/v1beta/extract"
_MAX_CONTENT_CHARS = 50_000


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
        # Disable automatic redirects so we can validate each hop.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            for url in urls:
                try:
                    resp = await self._safe_get(client, url)
                    resp.raise_for_status()

                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        text = converter.handle(resp.text)
                    else:
                        text = resp.text

                    results.append(
                        {
                            "url": url,
                            "title": _extract_title(resp.text)
                            if "text/html" in content_type
                            else url,
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
    async def _safe_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET *url* with SSRF validation, manually following redirects.
        
        Validates URL and each redirect target before making the request.
        Uses pre-resolved IPs to prevent DNS TOCTOU attacks (DNS rebinding).
        Does not expose internal IP addresses to the user.
        """
        for _ in range(_MAX_REDIRECTS):
            # Validate URL and get the resolved IP to eliminate DNS TOCTOU window
            resolved_ip, port = await _validate_url(url)
            
            parsed = urlparse(url)
            hostname = parsed.hostname
            scheme = parsed.scheme
            
            # Rewrite URL to use the resolved IP directly, preserving the path/query
            # This ensures the IP we validated is the one actually used.
            # The Host header tells the server which hostname we're requesting.
            ip_url = f"{scheme}://{resolved_ip}:{port}{parsed.path or ''}"
            if parsed.query:
                ip_url += f"?{parsed.query}"
            if parsed.fragment:
                ip_url += f"#{parsed.fragment}"
            
            resp = await client.get(
                ip_url,
                headers={"Host": hostname}
            )
            
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError("Redirect with no Location header")
                # Resolve relative redirects against the current URL (original URL with hostname).
                url = str(httpx.URL(url).join(location))
                continue
            return resp
        raise ValueError("Too many redirects")

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
