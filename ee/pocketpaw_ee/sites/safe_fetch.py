# ee/pocketpaw_ee/sites/safe_fetch.py — the ONE SSRF-hardened outbound fetch (SF-7).
#
# Created 2026-09-18 (feat/sites-single-url-fetch): extracted VERBATIM out of
# ee/pocketpaw_ee/sites/url_crawler.py, which had grown the only hardened egress
# path in the sites codebase and then buried it inside a BFS crawler. Nothing
# here is new behaviour — the guards, the error codes and the messages are the
# crawler's, moved so a caller that needs exactly ONE fetch can have it without
# abusing a depth-1 crawl or hand-rolling a second, weaker fetch. url_crawler
# now imports these names and re-exports its historical aliases
# (validate_fetch_url, FetchError, FetchBudgetExceeded), so its callers and its
# test suite are untouched.
#
# WHY IT MATTERS THAT THERE IS ONLY ONE. Each guard below closes a specific SSRF
# technique, and a second fetch path re-opens whichever one it forgets:
#   * URL shape (``validate_fetch_url``): http(s) only, a real hostname, NO
#     credentials in the URL, NO ports beyond 80/443/default, length-capped. A
#     LITERAL-IP host is run through the forbidden-IP check right here, so
#     ``http://169.254.169.254/`` dies before any socket is opened.
#   * DNS is resolved HERE and the connection is PINNED to the validated IP (the
#     request rides ``scheme://ip/...`` with the original Host header + the SNI
#     hostname for https), so a re-resolution between check and fetch
#     (TOCTOU / DNS rebinding) cannot swap in a private address.
#   * ALL resolved addresses must pass — a mixed public+private answer is
#     rejected outright, NOT filtered down to the public record. Filtering would
#     hand an attacker who controls DNS a public record to use as a passkey.
#   * Forbidden targets in both families: loopback, RFC1918/private, link-local
#     (incl. the 169.254.169.254 metadata address), CGNAT 100.64/10,
#     unspecified/reserved/multicast, IPv6 ULA fc00::/7, fe80::/10, and v4
#     addresses EMBEDDED in v6 forms (IPv4-mapped, 6to4, Teredo, NAT64
#     64:ff9b::/96) re-checked as the embedded v4.
#   * Redirects are followed MANUALLY (max ``MAX_REDIRECTS``) and EVERY hop
#     re-runs the full URL + DNS + IP validation; non-http(s) hops are rejected.
#   * Per-fetch timeout and a per-response size cap enforced ON THE STREAM — the
#     response is aborted the moment a cap is crossed, never buffered past it.
#   * No cookies (jar cleared after every response), no auth, no env proxies
#     (``trust_env=False`` — an HTTP_PROXY in the environment would route around
#     the pin), an honest User-Agent.
#
# The error CODES still read "sites.import_*". They are the crawler's, kept
# verbatim so this extraction changes no response body; a caller outside the
# import path should catch them and re-raise in its own vocabulary rather than
# re-word them here, because the import endpoint's 422 contract depends on these
# exact strings.

"""The single SSRF-hardened outbound fetch primitive for Paw Sites."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from pocketpaw_ee.cloud._core.errors import ValidationError

logger = logging.getLogger(__name__)

# Honest UA — robots groups for "pawsitesimporter" or "*" apply to us.
USER_AGENT = "PawSitesImporter/1.0 (+https://pocketpaw.dev; site-import crawler)"

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 5

# Per-response cap — one file may not eat the whole budget.
MAX_FETCH_BYTES = 10 * 1024 * 1024
PER_FETCH_TIMEOUT_SEC = 10.0

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_NAT64_V6 = ipaddress.ip_network("64:ff9b::/96")


class FetchError(RuntimeError):
    """A fetch failure with a FIXED, safe message (it lands in the import report,
    which viewers read — never raw upstream text, never a traceback)."""

    def __init__(self, message: str, *, code: str = "sites.import_crawl_failed") -> None:
        super().__init__(message)
        self.code = code


class FetchBudgetExceeded(FetchError):
    """The total byte budget was crossed — the caller fails closed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="sites.import_crawl_budget_exceeded")


# --------------------------------------------------------------------------- #
# URL + IP validation (the SSRF floors)
# --------------------------------------------------------------------------- #


def validate_fetch_url(url: str) -> Any:
    """Validate one fetch-target URL's SHAPE and return its ``urlparse`` result.

    Enforced (each raises ``ValidationError`` → 422 at the endpoint, a failed
    report in the background crawl): http/https only; a real hostname; NO
    credentials in the URL; NO ports beyond 80/443/default; length cap. A
    literal-IP host is additionally run through the forbidden-IP check here, so
    ``http://169.254.169.254/`` dies at validation, before any socket."""
    candidate = (url or "").strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        raise ValidationError("sites.import_url_invalid", "A non-empty http(s) URL is required.")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(
            "sites.import_url_invalid", "Only http and https URLs can be imported."
        )
    if not parsed.hostname:
        raise ValidationError("sites.import_url_invalid", "The import URL must carry a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError(
            "sites.import_url_forbidden", "URLs with embedded credentials are not allowed."
        )
    try:
        port = parsed.port  # raises ValueError on a malformed port
    except ValueError as exc:
        raise ValidationError(
            "sites.import_url_invalid", "The import URL port is invalid."
        ) from exc
    if port not in (None, 80, 443):
        raise ValidationError(
            "sites.import_url_forbidden",
            "Only the standard web ports (80/443) can be imported.",
        )
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _forbidden_ip_reason(literal)
        if reason:
            raise ValidationError(
                "sites.import_url_forbidden",
                f"The import URL points at a non-public address ({reason}).",
            )
    return parsed


def _forbidden_ip_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why this address may NOT be fetched, or None when it is publicly routable.

    v6 forms that EMBED a v4 address (IPv4-mapped, 6to4, Teredo, NAT64) are
    re-checked as the embedded v4 — ``::ffff:127.0.0.1`` is still loopback."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _forbidden_ip_reason(ip.ipv4_mapped)
        if ip.sixtofour is not None and _forbidden_ip_reason(ip.sixtofour):
            return "6to4-embedded private address"
        if ip.teredo is not None and any(_forbidden_ip_reason(a) for a in ip.teredo):
            return "teredo-embedded private address"
        if ip in _NAT64_V6:
            embedded = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
            if _forbidden_ip_reason(embedded):
                return "NAT64-embedded private address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_private:
        return "private address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return "carrier-grade NAT address"
    return None


async def _default_resolve(host: str) -> list[str]:
    """Resolve ``host`` to its addresses via the event loop's resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    return ips


# --------------------------------------------------------------------------- #
# The SSRF-pinned fetcher
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    """One completed (post-redirect) fetch."""

    url: str
    status: int
    content_type: str
    body: bytes


class SafeFetcher:
    """httpx-based fetcher that resolves DNS itself and pins the connection.

    Every ``fetch`` re-validates the URL shape, resolves the host, checks EVERY
    resolved address against the forbidden ranges, then connects to the validated
    IP with the original Host header (and SNI hostname for https) — the classic
    check-then-fetch TOCTOU is closed because the socket never re-resolves.
    Redirects are followed manually (max ``MAX_REDIRECTS``) with the full check
    re-run per hop. Responses stream against a per-fetch cap and a shared total
    byte budget. ``transport`` / ``resolver`` are test seams (MockTransport +
    a fake resolver — tests never touch the network)."""

    def __init__(
        self,
        *,
        total_byte_cap: int,
        per_fetch_cap: int = MAX_FETCH_BYTES,
        timeout_sec: float = PER_FETCH_TIMEOUT_SEC,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._total_byte_cap = total_byte_cap
        self._per_fetch_cap = per_fetch_cap
        self._resolver = resolver or _default_resolve
        self.bytes_fetched = 0
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_sec),
            follow_redirects=False,  # hops are validated manually
            trust_env=False,  # no env proxies — the pin must not be bypassed
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _checked_ip(self, host: str) -> str:
        """Resolve ``host`` and return a validated connect address. ALL resolved
        addresses must be public — one private record fails the whole host."""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            reason = _forbidden_ip_reason(literal)
            if reason:
                raise ValidationError(
                    "sites.import_url_forbidden",
                    f"Crawl target resolves to a non-public address ({reason}).",
                )
            return str(literal)
        try:
            ips = await self._resolver(host)
        except (OSError, socket.gaierror) as exc:
            raise FetchError(
                "DNS resolution failed for the crawl target",
                code="sites.import_crawl_dns_failed",
            ) from exc
        if not ips:
            raise FetchError(
                "DNS resolution returned no addresses for the crawl target",
                code="sites.import_crawl_dns_failed",
            )
        for raw in ips:
            try:
                addr = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise FetchError(
                    "DNS resolution returned an unparseable address",
                    code="sites.import_crawl_dns_failed",
                ) from exc
            reason = _forbidden_ip_reason(addr)
            if reason:
                raise ValidationError(
                    "sites.import_url_forbidden",
                    f"Crawl target resolves to a non-public address ({reason}).",
                )
        return ips[0]

    async def fetch(self, url: str, *, allowed_host: str | None = None) -> FetchResult:
        """GET ``url`` with the full SSRF pipeline, following redirects manually.

        Every hop is SSRF-revalidated regardless. ``allowed_host`` adds an
        orthogonal SCOPE guard: when set, a redirect that leaves that host
        raises ``sites.import_crawl_offsite_redirect`` so a same-site asset/page
        can't 30x us into fetching (and deploying) foreign content. The seed
        fetch passes ``None`` — the caller re-seeds the crawl host from the
        final URL instead (so an apex->www redirect imports the whole site)."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            parsed = validate_fetch_url(current)
            ip = await self._checked_ip(parsed.hostname)
            status, content_type, location, body = await self._pinned_get(parsed, ip)
            if status in _REDIRECT_STATUSES:
                if not location:
                    raise FetchError("redirect response carried no Location header")
                current = urljoin(current, location)
                if allowed_host is not None and urlparse(current).netloc.lower() != allowed_host:
                    raise FetchError(
                        f"redirect left the site ({allowed_host} -> "
                        f"{urlparse(current).netloc.lower()})",
                        code="sites.import_crawl_offsite_redirect",
                    )
                continue
            return FetchResult(url=current, status=status, content_type=content_type, body=body)
        raise FetchError(f"too many redirects (max {MAX_REDIRECTS})")

    async def _pinned_get(self, parsed: Any, ip: str) -> tuple[int, str, str, bytes]:
        """One GET pinned to ``ip``: URL host swapped for the validated address,
        original Host header (and SNI hostname for https) supplied explicitly."""
        port = parsed.port
        default_port = port is None or (parsed.scheme, port) in (("http", 80), ("https", 443))
        host_header = parsed.hostname if default_port else f"{parsed.hostname}:{port}"
        ip_host = f"[{ip}]" if ":" in ip else ip
        netloc = ip_host if default_port else f"{ip_host}:{port}"
        pinned = urlunparse((parsed.scheme, netloc, parsed.path or "/", "", parsed.query, ""))
        request = self._client.build_request("GET", pinned, headers={"Host": host_header})
        if parsed.scheme == "https":
            # TLS must negotiate + verify against the REAL name, not the IP.
            request.extensions["sni_hostname"] = parsed.hostname
        response = await self._client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self._per_fetch_cap:
                raise FetchError(
                    "response exceeds the per-fetch size cap",
                    code="sites.import_crawl_response_too_large",
                )
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > self._per_fetch_cap:
                    raise FetchError(
                        "response exceeds the per-fetch size cap",
                        code="sites.import_crawl_response_too_large",
                    )
                if self.bytes_fetched + len(buf) > self._total_byte_cap:
                    raise FetchBudgetExceeded("crawl exceeded the total byte budget")
        finally:
            await response.aclose()
            # No cookie persistence — the crawler is stateless by design.
            self._client.cookies.clear()
        self.bytes_fetched += len(buf)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        return response.status_code, content_type, response.headers.get("location", ""), bytes(buf)


# --------------------------------------------------------------------------- #
# The single-URL entry point
# --------------------------------------------------------------------------- #


async def fetch_single_url(
    url: str,
    *,
    max_bytes: int = MAX_FETCH_BYTES,
    timeout_sec: float = PER_FETCH_TIMEOUT_SEC,
    allowed_host: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
) -> FetchResult:
    """GET exactly ONE customer-supplied URL through the full SSRF pipeline.

    Call this when you need one fetch of an untrusted URL — a domain-ownership
    probe, a manifest read, an echo check. It owns the client lifecycle: one
    ``SafeFetcher`` is built, used once, and closed even when the fetch raises.

    A non-2xx upstream is NOT an error here: the status is returned on the
    ``FetchResult`` and the caller decides. What IS an error — the raise-on-
    reject contract:

    * ``ValidationError`` — the URL was REJECTED. Bad shape (non-http(s),
      embedded credentials, a port other than 80/443/default, over-long) or a
      forbidden target (a literal private IP, or a host whose DNS answer
      contains ANY non-public address — a mixed answer is rejected, never
      filtered). ``.code`` is ``sites.import_url_invalid`` or
      ``sites.import_url_forbidden``. A caller with its own error vocabulary
      should catch this and re-raise in its own terms.
    * ``FetchError`` — the fetch itself failed closed. DNS failure
      (``sites.import_crawl_dns_failed``), a response over ``max_bytes``
      (``sites.import_crawl_response_too_large``, raised MID-STREAM so an
      oversized body is never fully buffered), more than ``MAX_REDIRECTS``
      hops, a redirect carrying no Location, or — with ``allowed_host`` set —
      a redirect that leaves that host
      (``sites.import_crawl_offsite_redirect``).
    * ``httpx.HTTPError`` — a transport-level failure (connection refused,
      timeout, TLS). Deliberately NOT wrapped: the caller's retry policy wants
      to see it.

    ``allowed_host`` is the optional off-host redirect guard. Leave it None to
    follow a redirect anywhere that passes the SSRF checks (every hop is still
    fully revalidated), or pass a lowercased netloc to pin the whole hop chain
    to one host. ``transport``/``resolver`` are the test seams: pass an
    ``httpx.MockTransport`` and a dict-backed resolver and the call never opens
    a socket.
    """
    fetcher = SafeFetcher(
        total_byte_cap=max_bytes,
        per_fetch_cap=max_bytes,
        timeout_sec=timeout_sec,
        transport=transport,
        resolver=resolver,
    )
    try:
        return await fetcher.fetch(url, allowed_host=allowed_host)
    finally:
        await fetcher.aclose()
