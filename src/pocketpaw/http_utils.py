"""HTTP request helpers shared across API and dashboard modules."""

from __future__ import annotations

from fastapi import Request


def is_request_secure(request: Request) -> bool:
    """Return True when the original request protocol is HTTPS.

    Trust note: forwarded headers are only reliable when PocketPaw is deployed
    behind a trusted reverse proxy/tunnel that overwrites these headers.
    """
    if request.url.scheme == "https":
        return True

    raw_forwarded_proto = request.headers.get("x-forwarded-proto")
    if raw_forwarded_proto:
        first_hop_proto = raw_forwarded_proto.split(",", maxsplit=1)[0].strip().lower()
        if first_hop_proto == "https":
            return True

    # RFC 7239 `Forwarded` header (example: Forwarded: for=1.2.3.4;proto=https)
    raw_forwarded = request.headers.get("forwarded")
    if not raw_forwarded:
        return False

    first_forwarded_entry = raw_forwarded.split(",", maxsplit=1)[0]
    for item in first_forwarded_entry.split(";"):
        key, _, value = item.partition("=")
        if key.strip().lower() != "proto":
            continue
        proto = value.strip().strip('"').lower()
        return proto == "https"

    return False
