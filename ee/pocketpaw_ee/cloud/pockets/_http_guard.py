# _http_guard.py — The canonical SSRF-guard module for pocket HTTP executors.
# Created: 2026-05-22 (RFC 05 M2a) — EXTRACTED verbatim from
#   `source_executor.py` (RFC 04 alpha). It is the ONE shared module both the
#   read executor (`source_executor.py`) and the write executor
#   (`action_executor.py`) import for outbound-HTTP safety. Pure extraction —
#   no behavior change from the alpha; the read-executor regression tests are
#   the proof gate.
#
# SSRF BOUNDARY. Every defense from the locked RFC 04 security review lives
# here: strict path-traversal rejection, absolute-URL rejection, same-host
# assertion after URL join, DNS rebinding check, no redirect following,
# tight timeouts, a 512 KB response cap, error-message sanitization, and the
# auth-header builder. The executors layer their own policy (rate limits,
# allowlist, instinct gating) ON TOP of these primitives.
#
# IMPORT-LINTER: must NOT import `pocketpaw_ee.cloud.models.*`. This module
# only sees base_url / auth / path strings passed by parameter.

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import urllib.parse

import httpx

from pocketpaw.security.url_validators import host_is_internal

# --- limits / policy --------------------------------------------------------
_MAX_RESPONSE_BYTES = 524_288  # 512 KB (D11)
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)  # D10


class _GuardError(Exception):
    """An HTTP-guard failure with an already-sanitized message.

    Both executors catch this and fold ``message`` / ``code`` into their
    own ``{ok: false}`` / per-source error shapes — the raw exception text
    is never propagated to a client.
    """

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _strip_query(url: str) -> str:
    """Return ``url`` with query string and fragment removed — safe to log."""
    return urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()


def _resolve_url(base_url: str, path: str) -> str:
    """Join ``path`` onto ``base_url`` and reject anything that escapes it.

    Implements D7: reject absolute URLs, ``..`` segments, null bytes /
    non-printable chars, and any join whose resulting netloc differs from
    the base. Returns the safe absolute URL.
    """
    if "\x00" in path or any(not ch.isprintable() for ch in path):
        raise _GuardError("path contains illegal characters", code="bad_path")

    split_path = urllib.parse.urlsplit(path)
    if split_path.scheme or split_path.netloc:
        raise _GuardError("path must be relative, not an absolute URL", code="bad_path")

    # Percent-decode and reject traversal segments.
    decoded = urllib.parse.unquote(split_path.path)
    if any(seg == ".." for seg in decoded.split("/")):
        raise _GuardError("path may not contain '..' segments", code="bad_path")

    joined = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if urllib.parse.urlsplit(joined).netloc != urllib.parse.urlsplit(base_url).netloc:
        raise _GuardError("path resolves to a different host", code="bad_path")
    return joined


async def _assert_host_external(hostname: str) -> None:
    """D8 — resolve ``hostname`` and reject if any IP is internal.

    Guards against DNS rebinding: the base URL may be a public name that
    resolves to a private address. ``getaddrinfo`` runs in a worker thread
    so the event loop is not blocked.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
    except socket.gaierror as exc:
        raise _GuardError("backend host could not be resolved", code="dns_error") from exc

    for info in infos:
        # info[4] is the sockaddr — (host, port[, flowinfo, scope_id]).
        ip = str(info[4][0])
        # strip a zone id (fe80::1%eth0) before parsing
        ip = ip.split("%", 1)[0]
        if host_is_internal(ip):
            raise _GuardError("backend host resolves to an internal address", code="bad_host")
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise _GuardError("backend host resolves to an internal address", code="bad_host")


def _auth_headers(auth_type: str, auth_header: str | None, token: str) -> dict[str, str]:
    """Build the request auth header for the configured auth type.

    ``none`` adds no header. Unknown types are treated as ``none`` —
    the DTO Literal already constrains the wire input.

    For ``basic`` the stored token is the raw ``user:pass`` credential; it
    is base64-encoded here to form a valid ``Authorization: Basic`` header.
    """
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "api_key":
        return {(auth_header or "X-Api-Key"): token}
    if auth_type == "basic":
        encoded = base64.b64encode(token.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {}


__all__ = [
    "_HTTP_TIMEOUT",
    "_MAX_RESPONSE_BYTES",
    "_GuardError",
    "_assert_host_external",
    "_auth_headers",
    "_resolve_url",
    "_strip_query",
]
