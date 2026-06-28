# URL validators for Settings fields — guards against SSRF via config.
# Added: 2026-04-16 for security cluster E (#703).
# Updated: 2026-05-21 (RFC 04 alpha) — added validate_external_url_strict():
#   https-only, unconditionally blocks internal/loopback/RFC1918/link-local
#   hosts (no POCKETPAW_ALLOW_INTERNAL_URLS escape hatch), rejects empty
#   input. Used to validate pocket-backend base URLs, which are an SSRF
#   boundary and must never be relaxed by an operator env flag.
# Updated: 2026-05-21 (PR #1177 security pass) — exposed a public
#   ``host_is_internal`` alias and an ``__all__`` so callers no longer
#   import the private ``_host_is_internal`` symbol.
# Updated: 2026-06-28 (AW-1 connector egress guard) — added the OSS-side
#   ``assert_egress_allowed(url, allowed_hosts)`` egress primitive and the
#   pinned-IP ``PinnedTransport``. ``assert_egress_allowed`` enforces
#   https-only, rejects userinfo (``@``) and fragments, requires the host to
#   be in ``allowed_hosts``, DNS-resolves once, and rejects any resolved IP
#   that is internal (reusing ``host_is_internal`` + the IP-class check the
#   pocket guard already uses). It returns an ``EgressTarget`` carrying the
#   resolved/pinned IP. ``PinnedTransport`` connects to that pinned IP while
#   preserving the original Host header + TLS SNI, so there is no second DNS
#   lookup between the check and the connect — closing the DNS-rebind TOCTOU.
#   The EE ``_http_guard`` re-exports these so it stays the canonical entry
#   point; the OSS connector engine imports them directly (the OSS->EE import
#   boundary forbids importing ``pocketpaw_ee`` from core). When
#   ``POCKETPAW_ALLOW_INTERNAL_URLS`` is set (dev escape) internal resolved
#   IPs are permitted so localhost connectors keep working in development.

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Pre-load .env into os.environ at import time. Without this,
# POCKETPAW_ALLOW_INTERNAL_URLS set in .env is only read by pydantic-settings
# into Settings fields — it never reaches os.environ, so the validator below
# (which uses os.getenv) would miss the opt-in and block every localhost URL
# even when the operator set the flag. python-dotenv is an indirect dep via
# pydantic-settings; fall back silently if it's somehow unavailable.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(override=False)
except Exception:  # pragma: no cover — dotenv is optional
    pass

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Loopback + link-local + RFC1918 + carrier-grade NAT — allowed by default
# because PocketPaw is a self-hosted agent whose common path is talking to
# local services (Ollama, LiteLLM, opencode). Operators loading config from
# untrusted sources should set ``POCKETPAW_ALLOW_INTERNAL_URLS=false`` to
# re-enable the SSRF guard.
_BLOCKED_HOSTS: frozenset[str] = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / EC2 metadata
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


_TRUTHY = {"1", "true", "yes", "on"}


def _read_dotenv_flag() -> str | None:
    # pydantic-settings loads .env into the Settings object, not os.environ,
    # so a field-level validator can't see flags set there. Fall back to
    # parsing .env directly (cwd, then backend root) for this single flag.
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip() == "POCKETPAW_ALLOW_INTERNAL_URLS":
                        return val.strip().strip("\"'")
        except OSError:
            continue
    return None


def _allow_internal() -> bool:
    val = os.getenv("POCKETPAW_ALLOW_INTERNAL_URLS")
    if val is None:
        val = _read_dotenv_flag()
    if val is None:
        return True
    return val.strip().lower() in _TRUTHY


def host_is_internal(host: str) -> bool:
    """Return True when ``host`` is loopback / RFC1918 / link-local / CGNAT.

    Public entry point for the host classification used by the SSRF guards.
    A bare hostname (not an IP literal) returns False — name resolution is
    the caller's job.
    """
    host = host.lower().strip("[]")
    if host in _BLOCKED_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _BLOCKED_NETWORKS)


# Back-compat alias — kept for callers that imported the private name.
_host_is_internal = host_is_internal


def validate_external_url(value: str) -> str:
    """Pydantic validator for Settings URL fields.

    * Empty string is passed through — means "not configured" in this codebase.
    * Scheme must be ``http`` or ``https``.
    * Loopback / RFC1918 / link-local / carrier-grade NAT hosts are allowed
      by default; set ``POCKETPAW_ALLOW_INTERNAL_URLS=false`` to block them.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(f"URL must be a string, got {type(value).__name__}")

    parts = urlsplit(value)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme '{parts.scheme or '(none)'}' not allowed — use http or https")
    if not parts.hostname:
        raise ValueError(f"URL has no host: {value!r}")

    if host_is_internal(parts.hostname) and not _allow_internal():
        raise ValueError(
            f"URL host '{parts.hostname}' is internal/loopback/private and "
            f"POCKETPAW_ALLOW_INTERNAL_URLS is set to false"
        )
    return value


def validate_external_url_strict(value: str) -> str:
    """Strict external-URL validator for pocket backend base URLs.

    Differs from :func:`validate_external_url` in three ways, because a
    pocket-backend base URL is an SSRF boundary and must not be relaxed:

    * ``https://`` ONLY — plain ``http://`` is rejected.
    * Internal / loopback / RFC1918 / link-local / CGNAT hosts are blocked
      UNCONDITIONALLY — there is no ``POCKETPAW_ALLOW_INTERNAL_URLS`` escape
      hatch (this function never reads that flag).
    * Empty / blank input raises ``ValueError`` instead of passing through.

    Returns the URL unchanged when it passes; raises ``ValueError`` otherwise.
    """
    if value is None or not isinstance(value, str) or value.strip() == "":
        raise ValueError("URL must be a non-empty string")

    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError(
            f"URL scheme '{parts.scheme or '(none)'}' not allowed — backend URLs must use https"
        )
    if not parts.hostname:
        raise ValueError(f"URL has no host: {value!r}")
    if host_is_internal(parts.hostname):
        raise ValueError(
            f"URL host '{parts.hostname}' is internal/loopback/private — "
            f"backend URLs must point to an external host"
        )
    return value


# ---------------------------------------------------------------------------
# Egress guard primitive (AW-1) — shared by the OSS connector engine and the
# EE pocket HTTP guard (which re-exports it). Closes the connector SSRF bypass
# by enforcing an allow-list + a DNS-rebind-safe pinned-IP transport.
# ---------------------------------------------------------------------------


class EgressError(ValueError):
    """An egress-guard rejection. Subclasses ``ValueError`` so existing
    ``except ValueError`` handlers around URL validation keep working."""


@dataclass(frozen=True)
class EgressTarget:
    """The resolved, allow-listed target of an outbound request.

    ``url`` is the original (validated) URL — unchanged, so the Host header
    and TLS SNI stay correct. ``host`` is its hostname; ``port`` the resolved
    port (scheme default applied). ``pinned_ip`` is the single IP the
    connection MUST go to — :class:`PinnedTransport` dials it directly so no
    second DNS lookup can race the check (the DNS-rebind TOCTOU).
    """

    url: str
    host: str
    port: int
    pinned_ip: str


def _ip_is_internal(ip: str) -> bool:
    """True when ``ip`` (a literal) is loopback/private/link-local/reserved.

    Layers the explicit ``host_is_internal`` block-list (RFC1918, CGNAT,
    metadata link-local, …) with Python's ``ipaddress`` classification so
    ranges the static list misses (e.g. multicast/reserved) are also caught.
    """
    if host_is_internal(ip):
        return True
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_ips(host: str) -> list[str]:
    """Resolve ``host`` to its IP literals. Raises ``EgressError`` on failure."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise EgressError(f"host '{host}' could not be resolved") from exc
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]  # strip a zone id (fe80::1%eth0)
        if ip not in ips:
            ips.append(ip)
    return ips


async def assert_egress_allowed(url: str, allowed_hosts: set[str] | frozenset[str]) -> EgressTarget:
    """Validate an outbound URL against the egress policy and pin its IP.

    Enforces, in order:

    * ``https://`` only — plain http is rejected (an attacker who can force
      http downgrades the channel and any allow-list bypass via redirect).
    * No userinfo (``user:pass@host``) and no fragment — both are classic
      allow-list-confusion vectors (the real host can hide after the ``@``).
    * The hostname MUST be in ``allowed_hosts`` (compared case-insensitively).
    * DNS resolves the host and EVERY resolved IP is rejected if internal
      (loopback / RFC1918 / link-local / metadata / reserved), unless the dev
      escape ``POCKETPAW_ALLOW_INTERNAL_URLS`` permits internal hosts.

    Returns an :class:`EgressTarget` carrying the single pinned IP. The caller
    MUST dial that IP via :class:`PinnedTransport` so the connection cannot be
    re-resolved to a different (internal) address between this check and the
    connect — that gap is the DNS-rebinding TOCTOU this guard exists to close.

    Raises :class:`EgressError` (a ``ValueError``) on any policy violation.
    """
    if not isinstance(url, str) or not url.strip():
        raise EgressError("URL must be a non-empty string")

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise EgressError(
            f"URL scheme '{parts.scheme or '(none)'}' not allowed — egress is https-only"
        )
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise EgressError("URL must not contain userinfo (user:pass@host)")
    if parts.fragment:
        raise EgressError("URL must not contain a fragment")
    host = parts.hostname
    if not host:
        raise EgressError(f"URL has no host: {url!r}")

    normalized = {h.lower() for h in allowed_hosts}
    if host.lower() not in normalized:
        raise EgressError(f"host '{host}' is not in the egress allow-list")

    # Resolve off the event loop; getaddrinfo blocks.
    ips = await asyncio.to_thread(_resolve_ips, host)
    if not ips:
        raise EgressError(f"host '{host}' resolved to no addresses")

    allow_internal = _allow_internal()
    for ip in ips:
        if _ip_is_internal(ip) and not allow_internal:
            raise EgressError(f"host '{host}' resolves to an internal address")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    # Pin the first resolved address. Every resolved IP already passed the
    # internal-range check above, so whichever we pin is allow-listed.
    return EgressTarget(url=url, host=host, port=port, pinned_ip=ips[0])


_PINNED_TRANSPORT_CLS: type | None = None


def _build_pinned_transport_cls() -> type:
    """Build (once) the ``httpx.AsyncBaseTransport`` subclass for pinning.

    Defined lazily so importing this module (which ``config.py`` imports at
    startup) never requires httpx at import time — httpx is a hard dependency
    but the class is only needed once an egress request is actually made.
    httpx must subclass ``AsyncBaseTransport`` for ``AsyncClient(transport=...)``
    to route requests through it.
    """
    global _PINNED_TRANSPORT_CLS
    if _PINNED_TRANSPORT_CLS is not None:
        return _PINNED_TRANSPORT_CLS

    import httpx

    class _PinnedTransport(httpx.AsyncBaseTransport):
        """An httpx async transport that dials a pre-resolved (pinned) IP.

        On each request it repoints ONLY the connection target to ``pinned_ip``
        (by rewriting the request URL's host), while preserving the original
        ``Host`` header and setting the ``sni_hostname`` extension to the real
        hostname so TLS SNI + certificate validation run against the name, not
        the IP. The bytes therefore go to the exact IP
        :func:`assert_egress_allowed` already vetted — no second DNS lookup that
        a rebinding attacker could race (the DNS-rebind TOCTOU).
        """

        def __init__(self, pinned_ip: str, *, verify: bool = True) -> None:
            self._pinned_ip = pinned_ip
            self._inner = httpx.AsyncHTTPTransport(verify=verify)

        async def handle_async_request(self, request: Any) -> Any:
            # httpx fixes the Host header at request-build time from the
            # original URL host, so capture it before rewriting the URL.
            original_host = request.url.host
            # Repoint the connect target at the pinned IP (httpx brackets an
            # IPv6 literal automatically). The Host header is left unchanged.
            request.url = request.url.copy_with(host=self._pinned_ip)
            request.headers["Host"] = original_host
            # TLS handshake + cert validation use sni_hostname (read by
            # httpcore), so the cert is verified against the real hostname,
            # not the pinned IP.
            request.extensions = dict(request.extensions or {})
            request.extensions["sni_hostname"] = original_host
            return await self._inner.handle_async_request(request)

        async def aclose(self) -> None:
            await self._inner.aclose()

    _PINNED_TRANSPORT_CLS = _PinnedTransport
    return _PinnedTransport


def PinnedTransport(pinned_ip: str, *, verify: bool = True) -> Any:  # noqa: N802
    """Return a pinned-IP ``httpx.AsyncBaseTransport`` for ``pinned_ip``.

    A factory (spelled like a class for call-site readability) that defers the
    httpx import + subclass construction until first use. Pass the result as
    ``httpx.AsyncClient(transport=PinnedTransport(ip))`` so every request on
    that client dials the vetted IP instead of re-resolving the hostname.
    """
    cls = _build_pinned_transport_cls()
    return cls(pinned_ip, verify=verify)


__all__ = [
    "host_is_internal",
    "validate_external_url",
    "validate_external_url_strict",
    "EgressError",
    "EgressTarget",
    "assert_egress_allowed",
    "PinnedTransport",
]
