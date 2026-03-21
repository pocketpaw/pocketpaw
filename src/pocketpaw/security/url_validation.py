"""URL validation helpers — SSRF prevention for config and tool boundaries.

Validates that URLs use safe schemes and do not target private/reserved
IP ranges.  Used by ``config.py`` field validators and tool-level checks
in ``url_extract`` and ``browser``.

See also: OWASP A10:2021 — Server-Side Request Forgery (SSRF).
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Private and reserved IPv4/IPv6 networks that should never be targeted
# by user-configured or agent-provided URLs.
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local / cloud metadata
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("::1/128"),
    ipaddress.IPv6Network("fc00::/7"),  # Unique local addresses
    ipaddress.IPv6Network("fe80::/10"),  # Link-local
]

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def validate_url(
    url: str,
    *,
    allow_localhost: bool = False,
    allow_private: bool = False,
) -> str:
    """Validate a URL for safe use in HTTP requests.

    Args:
        url: The URL string to validate.
        allow_localhost: If ``True``, permit ``127.0.0.0/8`` and ``::1``
            (needed for config fields whose defaults point at localhost).
        allow_private: If ``True``, skip all private-network checks
            (escape hatch for fully trusted environments).

    Returns:
        The original *url* string unchanged (for use as a Pydantic validator).

    Raises:
        ValueError: When the URL fails validation.
    """
    if not url or not url.strip():
        return url  # Empty strings are allowed (field is optional / unset)

    parsed = urlparse(url)

    # -- Scheme check --
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme must be http or https, got {parsed.scheme!r}: {url}")

    # -- Hostname check --
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no hostname: {url}")

    if allow_private:
        return url

    # -- IP-based host check --
    try:
        addr = ipaddress.ip_address(host)
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                if allow_localhost and addr.is_loopback:
                    return url
                raise ValueError(f"URL targets a private/reserved address ({host}): {url}")
    except ValueError as exc:
        # Re-raise our own ValueErrors (from the block above)
        if "URL" in str(exc):
            raise
        # Not a literal IP — it's a hostname, which is acceptable
        pass

    return url


def is_url_safe(url: str, *, allow_localhost: bool = False) -> bool:
    """Non-raising variant — returns ``False`` when the URL is unsafe."""
    try:
        validate_url(url, allow_localhost=allow_localhost)
        return True
    except ValueError:
        return False
