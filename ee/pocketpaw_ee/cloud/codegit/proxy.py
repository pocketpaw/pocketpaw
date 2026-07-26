# proxy.py — the Code Mode git smart-HTTP reverse proxy (CM-3d).
# Created 2026-07-16 (feat/code-mode): the token-injecting core. The sandbox VM
# speaks git smart-HTTP to us (authenticated by a sandbox+repo ticket, never the
# GitHub token); we mint a fresh, repo-scoped GitHub installation token
# SERVER-SIDE and stream the request upstream to github.com with that token in
# the ``Authorization`` header. The token is only ever in our outbound request —
# it is never written to a VM, a log, or the response.
#
# Only the two git smart-HTTP services are allowed — ``git-upload-pack`` (fetch)
# and ``git-receive-pack`` (push). The upstream host is ALWAYS the configured git
# base (github.com), with ``owner``/``repo`` interpolated as single, validated
# path segments, so a crafted path can't redirect the proxy at another host
# (SSRF) or another repo. The ticket already pins the repo; we re-check the URL
# repo against it as defense-in-depth.
#
# Bodies (packfiles) can be large, so both directions STREAM: the request body is
# forwarded as the incoming async byte stream, and the upstream response is
# streamed straight back. ``http_client`` is injectable so tests exercise the
# forwarding + token injection against a fake github with no network.

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from starlette.responses import StreamingResponse

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, Forbidden
from pocketpaw_ee.cloud.codegit.ticket import TicketClaims
from pocketpaw_ee.cloud.websandbox import broker

logger = logging.getLogger(__name__)

_GITHUB_GIT_BASE = "https://github.com"
# The two git smart-HTTP services. Anything else (receive-pack variants, custom
# services) is rejected — we proxy fetch + push, nothing more.
ALLOWED_SERVICES = frozenset({"git-upload-pack", "git-receive-pack"})

# owner / repo are single path segments — GitHub names allow letters, digits,
# ``.``, ``_`` and ``-``. Anything else (slash, ``..``, ``@``) is rejected before
# it can reach the upstream URL.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# Request headers we forward upstream (lower-cased). The incoming Authorization
# (the basic-auth ticket) is deliberately NOT here — we replace it with the
# minted GitHub token. Content-Length is dropped so httpx frames the stream.
_FORWARD_REQUEST_HEADERS = frozenset(
    {"content-type", "content-encoding", "accept", "accept-encoding", "git-protocol", "user-agent"}
)
# Response headers we pass back to the VM's git. Hop-by-hop + length headers are
# dropped (the body is re-streamed, so Transfer-Encoding is set fresh).
_FORWARD_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-encoding", "cache-control", "pragma", "expires", "www-authenticate"}
)

_UPSTREAM_TIMEOUT_SECONDS = 120.0


def _git_base() -> str:
    return os.environ.get("GITHUB_GIT_BASE", _GITHUB_GIT_BASE).strip().rstrip("/")


def _validate(owner: str, repo: str, service: str, claims: TicketClaims) -> None:
    """Reject unsafe segments, a disallowed service, or a repo the ticket doesn't cover."""
    if not _SEGMENT.match(owner) or not _SEGMENT.match(repo):
        raise BadRequest("codegit.bad_repo", "Malformed repository path")
    if service not in ALLOWED_SERVICES:
        raise BadRequest("codegit.bad_service", f"Unsupported git service {service!r}")
    # The ticket is minted for exactly one repo; the URL must match it.
    if f"{owner}/{repo}" != claims.repo.removesuffix(".git"):
        raise Forbidden("codegit.repo_mismatch", "Ticket is not valid for this repository")


def _upstream_url(owner: str, repo: str, git_path: str, query: str) -> str:
    """Build the github.com smart-HTTP URL for ``git_path`` (info/refs carries a query)."""
    url = f"{_git_base()}/{owner}/{repo}.git/{git_path}"
    return f"{url}?{query}" if query else url


def _forward_request_headers(headers: dict[str, str], token: str) -> dict[str, str]:
    """Whitelist the git request headers and inject the GitHub token (replacing the ticket)."""
    fwd = {k: v for k, v in headers.items() if k.lower() in _FORWARD_REQUEST_HEADERS}
    fwd["Authorization"] = f"token {token}"
    return fwd


def _forward_response_headers(headers: Any) -> dict[str, str]:
    """Whitelist the upstream response headers git needs; drop hop-by-hop/length."""
    return {k: v for k, v in headers.items() if k.lower() in _FORWARD_RESPONSE_HEADERS}


async def _mint_token(claims: TicketClaims, owner: str, repo: str) -> str:
    """Mint a fresh, repo-scoped GitHub token for this push/fetch — server-side only.

    Reuses the broker's connection→token resolution: the token is minted from the
    caller's own connection that can reach the repo, with least-privilege write
    (contents + pull_requests). ``None`` means the connection is gone / revoked, so
    the git op is Forbidden rather than proxied with no auth.
    """
    repo_url = f"{_git_base()}/{owner}/{repo}.git"
    scoped = await broker.resolve_repo_token(claims.workspace_id, claims.user_id, repo_url)
    if scoped is None:
        raise Forbidden("codegit.no_repo_access", "No GitHub connection can access this repository")
    return scoped.token


async def proxy_git(
    *,
    claims: TicketClaims,
    owner: str,
    repo: str,
    git_path: str,
    service: str,
    method: str,
    request_headers: dict[str, str],
    request_body: AsyncIterator[bytes] | bytes,
    query: str = "",
    http_client: Any = None,
) -> StreamingResponse:
    """Proxy one git smart-HTTP request upstream with a minted token injected.

    Validates the repo/service against the ticket, mints a repo-scoped GitHub
    token server-side, and streams the request to ``github.com/<owner>/<repo>.git/
    <git_path>`` with ``Authorization: token …``. The upstream response is streamed
    straight back to the VM's git. The GitHub token never appears in the response.
    ``http_client`` (an ``httpx.AsyncClient``) is injected in tests.
    """
    _validate(owner, repo, service, claims)
    token = await _mint_token(claims, owner, repo)

    url = _upstream_url(owner, repo, git_path, query)
    fwd_headers = _forward_request_headers(request_headers, token)

    owns_client = http_client is None
    if owns_client:
        import httpx

        http_client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_SECONDS)

    try:
        request = http_client.build_request(method, url, headers=fwd_headers, content=request_body)
        upstream = await http_client.send(request, stream=True)
    except CloudError:
        if owns_client:
            await http_client.aclose()
        raise
    except Exception as exc:  # noqa: BLE001 — any upstream transport failure is uniform
        if owns_client:
            await http_client.aclose()
        raise CloudError(
            502, "codegit.upstream_failed", "The git host could not be reached"
        ) from exc

    async def _body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            if owns_client:
                await http_client.aclose()

    logger.info(
        "codegit.proxy: ws=%s repo=%s/%s %s -> upstream %s",
        claims.workspace_id,
        owner,
        repo,
        git_path,
        upstream.status_code,
    )
    return StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=_forward_response_headers(upstream.headers),
    )


__all__ = ["ALLOWED_SERVICES", "proxy_git"]
