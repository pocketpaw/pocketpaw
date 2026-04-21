# URL validators for Settings fields — guards against SSRF via config.
# Added: 2026-04-16 for security cluster E (#703).

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Loopback + link-local + RFC1918 + carrier-grade NAT — blocked unless the
# operator explicitly opts in via ``POCKETPAW_ALLOW_INTERNAL_URLS=true``.
# Dev-mode defaults (`http://localhost:4096`, `http://localhost:11434`, …)
# rely on this opt-in; OSS / production deployments should leave it off.
_BLOCKED_HOSTS: frozenset[str] = frozenset(
    {"localhost", "ip6-localhost", "ip6-loopback"}
)
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / EC2 metadata
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _allow_internal() -> bool:
    return os.getenv("POCKETPAW_ALLOW_INTERNAL_URLS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _host_is_internal(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in _BLOCKED_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_external_url(value: str) -> str:
    """Pydantic validator for Settings URL fields.

    * Empty string is passed through — means "not configured" in this codebase.
    * Scheme must be ``http`` or ``https``.
    * Loopback / RFC1918 / link-local / carrier-grade NAT hosts are blocked
      unless ``POCKETPAW_ALLOW_INTERNAL_URLS`` is set to a truthy value.
    """
    if value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(f"URL must be a string, got {type(value).__name__}")

    parts = urlsplit(value)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"URL scheme '{parts.scheme or '(none)'}' not allowed — use http or https"
        )
    if not parts.hostname:
        raise ValueError(f"URL has no host: {value!r}")

    if _host_is_internal(parts.hostname) and not _allow_internal():
        raise ValueError(
            f"URL host '{parts.hostname}' is internal/loopback/private — "
            f"set POCKETPAW_ALLOW_INTERNAL_URLS=true to permit it"
        )
    return value
