# SSRF-safe HTTP fetch primitives — DNS-pinned transport, redirect-hop
# validation, and a streaming byte-capped GET.
# Created: 2026-06-10 — extracted from tools/builtin/url_extract.py so both
#   the url_extract tool and the /api/v1/unfurl link-preview endpoint share
#   one hardened fetch path (IPPinningTransport defeats DNS rebinding by
#   resolving once, pinning the IP, and preserving Host/SNI; the SSRF check
#   re-runs on every redirect hop). Added safe_get_streamed() for callers
#   that need a body-size cap + final-URL tracking (link unfurl).

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECT_HOPS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


# Typed errors — all subclass ValueError so existing call sites that catch
# ValueError (url_extract's _extract_local) keep working unchanged, while
# new callers (the /unfurl endpoint) can map each class to the right HTTP
# status without string-matching the message.
class SafeFetchError(ValueError):
    """Base for any blocked / failed safe fetch."""


class UnsupportedSchemeError(SafeFetchError):
    """The URL scheme is not http/https (or the URL has no host)."""


class BlockedURLError(SafeFetchError):
    """The URL is SSRF-blocked — resolved to a non-public IP, or the
    redirect chain exceeded the hop cap."""


class FetchFailedError(SafeFetchError):
    """The fetch failed for a non-SSRF reason — DNS resolution failure,
    disallowed content-type, or a malformed redirect."""


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
        raise UnsupportedSchemeError("Blocked URL: only http and https URLs are allowed.")
    if not url.host:
        raise UnsupportedSchemeError("Blocked URL: hostname is required.")


def _port_for_url(url: httpx.URL) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def _validate_public_ip(raw_ip: str) -> str:
    parsed_ip = _normalize_ip_address(raw_ip)
    if not parsed_ip.is_global:
        raise BlockedURLError("Blocked URL: resolved to non-public IP address.")

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
        raise FetchFailedError("Could not resolve URL hostname.") from exc

    if not addrinfo:
        raise FetchFailedError("Could not resolve URL hostname.")

    candidate_ips: list[str] = []
    for record in addrinfo:
        sockaddr = record[4]
        if not sockaddr:
            continue
        raw_ip = sockaddr[0]
        candidate_ips.append(_validate_public_ip(raw_ip))

    if not candidate_ips:
        raise FetchFailedError("Could not resolve URL hostname.")

    return candidate_ips[0]


def _next_redirect_url(current_url: httpx.URL, location: str) -> httpx.URL:
    next_url = current_url.join(location)
    _ensure_supported_url(next_url)
    return next_url


async def safe_get(
    url: str,
    timeout: float = 30,
) -> httpx.Response:
    """Fetch a URL with SSRF protection, following redirects manually.

    Resolves DNS once per hop, validates the resolved IP is public, then
    connects to the pinned IP while preserving Host/SNI. Re-runs the SSRF
    check on every redirect hop (defeats DNS-rebinding TOCTOU). Reads the
    full response body. Raises ``ValueError`` on any blocked URL.
    """
    current_url = httpx.URL(url)
    _ensure_supported_url(current_url)

    for _ in range(_MAX_REDIRECT_HOPS + 1):
        if current_url.host is None:
            raise UnsupportedSchemeError("Blocked URL: hostname is required.")

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

        if response.status_code in _REDIRECT_STATUS:
            location = response.headers.get("location")
            if not location:
                return response

            current_url = _next_redirect_url(current_url, location)
            continue

        return response

    raise BlockedURLError("Blocked URL: too many redirects.")


class FetchResult:
    """Result of a streamed safe fetch.

    Attributes mirror only what link-preview callers need: the final URL
    after redirects, the response status, headers, content-type, and the
    (possibly truncated) decoded body text.
    """

    __slots__ = ("final_url", "status_code", "content_type", "text", "truncated")

    def __init__(
        self,
        *,
        final_url: str,
        status_code: int,
        content_type: str,
        text: str,
        truncated: bool,
    ) -> None:
        self.final_url = final_url
        self.status_code = status_code
        self.content_type = content_type
        self.text = text
        self.truncated = truncated


async def safe_get_streamed(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    allowed_content_types: tuple[str, ...] = ("text/html",),
) -> FetchResult:
    """Stream a URL with SSRF protection, capping the body at ``max_bytes``.

    Same SSRF guarantees as :func:`safe_get` (DNS pinned per hop, IP
    validated public, Host/SNI preserved, redirect cap, per-hop re-check),
    but streams the body so an oversized response is cut at ``max_bytes``
    instead of buffered whole. Validates the response content-type against
    ``allowed_content_types`` BEFORE reading the body — a non-matching type
    raises ``ValueError`` without downloading the payload.

    Returns a :class:`FetchResult` carrying the final URL (after redirects),
    so callers can resolve relative metadata URLs against it. Raises
    ``ValueError`` on any blocked URL, unsupported scheme, too many
    redirects, or disallowed content-type.
    """
    current_url = httpx.URL(url)
    _ensure_supported_url(current_url)

    for _ in range(_MAX_REDIRECT_HOPS + 1):
        if current_url.host is None:
            raise UnsupportedSchemeError("Blocked URL: hostname is required.")

        pinned_ip = await _resolve_public_ip(current_url.host, _port_for_url(current_url))

        transport = IPPinningTransport(
            pinned_ip=pinned_ip,
            original_host=current_url.host,
        )

        async with (
            httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=transport,
            ) as client,
            client.stream("GET", str(current_url)) as response,
        ):
            if response.status_code in _REDIRECT_STATUS:
                location = response.headers.get("location")
                if not location:
                    # A redirect status with no Location header — treat as
                    # a hard failure rather than serving an empty preview.
                    raise FetchFailedError("Blocked URL: redirect without Location header.")
                # Re-run the SSRF check on the next hop (defeats DNS-rebinding
                # TOCTOU). Closing the stream context, then continuing, picks
                # the new URL up on the next loop iteration.
                current_url = _next_redirect_url(current_url, location)
                continue

            content_type = response.headers.get("content-type", "")
            ct_main = content_type.split(";", 1)[0].strip().lower()
            if ct_main not in allowed_content_types:
                raise FetchFailedError(f"Blocked URL: unsupported content-type '{content_type}'.")

            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    truncated = True
                    break

            body = b"".join(chunks)[:max_bytes]
            # Decode using the response charset when known, else utf-8.
            encoding = response.charset_encoding or "utf-8"
            try:
                text = body.decode(encoding, errors="replace")
            except (LookupError, ValueError):
                text = body.decode("utf-8", errors="replace")

            return FetchResult(
                final_url=str(current_url),
                status_code=response.status_code,
                content_type=content_type,
                text=text,
                truncated=truncated,
            )

    raise BlockedURLError("Blocked URL: too many redirects.")


__all__ = [
    "BlockedURLError",
    "FetchFailedError",
    "FetchResult",
    "IPPinningTransport",
    "SafeFetchError",
    "UnsupportedSchemeError",
    "safe_get",
    "safe_get_streamed",
]
