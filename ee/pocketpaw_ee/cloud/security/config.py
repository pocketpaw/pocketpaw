# ee/pocketpaw_ee/cloud/security/config.py — shield connection config + UDS transport.
# Created: 2026-07-01 (feat/sec-5-security-proxy, SEC-5).
#
# Reads the shield connection settings from the central pocketpaw Settings
# (POCKETPAW_SHIELD_API_SOCKET / POCKETPAW_SHIELD_API_TOKEN) and builds the
# httpx AsyncClient bound to shield's UNIX socket. Kept out of the router so the
# router stays thin and tests can override ``shield_client_dep`` with a fake
# transport instead of a real socket. The Bearer token is attached to the
# client's default headers; it is NEVER logged (the router logs status codes and
# reasons only, never the token or the Authorization header).

from __future__ import annotations

import httpx

from pocketpaw.config import get_settings

# shield is on the same box; the socket round-trip is sub-millisecond when it is
# up. A tight timeout means a hung/slow shield fails fast into the "unreachable"
# degrade path instead of hanging the caller's request.
SHIELD_TIMEOUT_SECONDS: float = 5.0

# Base URL is a placeholder authority — httpx requires an absolute URL, but the
# UDS transport ignores the host and dials the socket. shield sees the path.
_SHIELD_BASE_URL = "http://shield"


def shield_socket_path() -> str:
    """Return the configured shield control-API socket path (may be empty)."""
    return (get_settings().shield_api_socket or "").strip()


def shield_api_token() -> str:
    """Return the Bearer token for the backend → shield hop. Never logged."""
    return (get_settings().shield_api_token or "").strip()


def build_shield_client() -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` bound to shield's UNIX socket.

    The caller is responsible for closing the client (use ``async with``). The
    Authorization header carrying the shield token is set as a default header so
    every request over this client authenticates. A tight timeout keeps a slow
    shield from hanging the request — a timeout surfaces to the router as the
    typed 'unreachable' degrade, not a 500.
    """
    transport = httpx.AsyncHTTPTransport(uds=shield_socket_path())
    headers = {"Authorization": f"Bearer {shield_api_token()}"}
    return httpx.AsyncClient(
        transport=transport,
        base_url=_SHIELD_BASE_URL,
        headers=headers,
        timeout=SHIELD_TIMEOUT_SECONDS,
    )
