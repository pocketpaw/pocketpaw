# URL validators for Settings fields — guards against SSRF via config.
# Added: 2026-04-16 for security cluster E (#703).

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
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

    if _host_is_internal(parts.hostname) and not _allow_internal():
        raise ValueError(
            f"URL host '{parts.hostname}' is internal/loopback/private and "
            f"POCKETPAW_ALLOW_INTERNAL_URLS is set to false"
        )
    return value
