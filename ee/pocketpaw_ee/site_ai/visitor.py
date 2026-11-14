# ee/pocketpaw_ee/site_ai/visitor.py — who a browser AI caller is, for tickets and caps.
#
# ``client_address``: the real visitor address behind our edge. The request chain is
# the ``X-Forwarded-For`` entries plus the socket peer; ``trusted_hops`` proxies of
# our own (Traefik: 1, ``POCKETPAW_SITE_AI_TRUSTED_HOPS``) each appended what they
# saw, so the address the outermost one saw is ``chain[-(hops + 1)]`` (the rightmost
# XFF entry with the default, as ``rate_limit.client_ip`` reads it). When THAT
# address is a Cloudflare edge, Cloudflare is in front and ``CF-Connecting-IP`` is
# the visitor; from anywhere else the header is ignored (anyone can send it).
#
# ``visitor_key``: HMAC-SHA256 of the address bucket with the ticket secret, so the
# per-visitor counters and ticket bindings carry no reversible IP. IPv6 is bucketed
# by /64 (one subscriber's allocation), IPv4 by the single address. Without a
# ticket secret (Worker-only deployments) a fixed-salt hash keeps the shape.

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from functools import lru_cache

from fastapi import Request

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/).
_CLOUDFLARE_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


@lru_cache(maxsize=1)
def _cloudflare_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(ipaddress.ip_network(r) for r in _CLOUDFLARE_RANGES)


def _ip(value: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def trusted_hops() -> int:
    raw = os.environ.get("POCKETPAW_SITE_AI_TRUSTED_HOPS", "1").strip()
    return max(0, min(int(raw), 5)) if raw.isdigit() else 1


def is_cloudflare(addr: ipaddress._BaseAddress) -> bool:
    return any(addr in net for net in _cloudflare_networks() if net.version == addr.version)


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else ""
    forwarded = [h for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    chain = [*forwarded, peer] if peer else forwarded
    if not chain:
        return "unknown"
    hops = trusted_hops()
    seen = chain[-(hops + 1)] if len(chain) > hops else chain[0]
    seen_ip = _ip(seen)
    if seen_ip is None:
        return peer or "unknown"
    if is_cloudflare(seen_ip):
        cf = _ip(request.headers.get("cf-connecting-ip", ""))
        if cf is not None:
            return str(cf)
    return str(seen_ip)


def bucket(address: str) -> str:
    addr = _ip(address)
    if addr is None:
        return address
    if addr.version == 6:
        mapped = addr.ipv4_mapped  # type: ignore[union-attr]
        if mapped is not None:
            return str(mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _key() -> bytes:
    from pocketpaw_ee.site_ai import tickets
    from pocketpaw_ee.site_ai.errors import AiError

    try:
        return hmac.new(tickets._secret(), b"site-ai-visitor-v1", hashlib.sha256).digest()
    except AiError:
        return b"site-ai-visitor-unkeyed"


def visitor_key(request: Request) -> str:
    return hmac.new(_key(), bucket(client_address(request)).encode(), hashlib.sha256).hexdigest()


__all__ = ["bucket", "client_address", "is_cloudflare", "trusted_hops", "visitor_key"]
