# router.py — FastAPI git smart-HTTP endpoints for the Code Mode proxy (CM-3d).
# Created 2026-07-16 (feat/code-mode): the three routes a git client hits over
# smart-HTTP, mounted at ``/api/v1/codegit``. The VM's git remote is
# ``https://x-paw-git:<ticket>@<backend>/api/v1/codegit/<owner>/<repo>``; git then
# requests ``…/info/refs`` (advertisement) and POSTs ``…/git-upload-pack`` (fetch)
# or ``…/git-receive-pack`` (push).
#
# Authentication is the signed ticket carried as the basic-auth PASSWORD — NOT a
# session cookie or bearer, so this router is deliberately OUTSIDE the normal
# license/RequestContext auth (git can't do OAuth). The CSRF middleware only fires
# for cookie auth, and the EE auth bridge only inspects Bearer, so a basic-auth
# git request passes both untouched and authenticates here via the ticket alone.
# proxy_git then mints the repo-scoped GitHub token server-side.

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from pocketpaw_ee.cloud.codegit.proxy import proxy_git
from pocketpaw_ee.cloud.codegit.ticket import TicketClaims, verify_ticket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/codegit", tags=["CodeGit"])

_REALM = 'Basic realm="paw-codegit"'


def _unauthorized() -> Response:
    """A 401 that prompts git to treat the ticket as invalid (no retry loop)."""
    return Response(status_code=401, headers={"WWW-Authenticate": _REALM})


def _ticket_from_basic_auth(request: Request) -> TicketClaims | None:
    """Recover the ticket claims from the basic-auth PASSWORD, or ``None``.

    The remote embeds the ticket as the password (``x-paw-git:<ticket>``); git
    sends it as ``Authorization: Basic base64(user:ticket)``. Any username is
    accepted — only the ticket (password) is verified.
    """
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    _user, _, password = decoded.partition(":")
    return verify_ticket(password)


@router.get("/{owner}/{repo}/info/refs")
async def info_refs(
    owner: str,
    repo: str,
    request: Request,
    service: str = Query(""),
) -> Response:
    """The ref advertisement — git's first request for both fetch and push."""
    claims = _ticket_from_basic_auth(request)
    if claims is None:
        return _unauthorized()
    return await proxy_git(
        claims=claims,
        owner=owner,
        repo=repo,
        git_path="info/refs",
        service=service,
        method="GET",
        request_headers=dict(request.headers),
        request_body=b"",
        query=f"service={service}",
    )


@router.post("/{owner}/{repo}/git-upload-pack")
async def upload_pack(owner: str, repo: str, request: Request) -> Response:
    """The fetch/clone pack negotiation (``git fetch``/``git pull``)."""
    return await _proxy_pack(owner, repo, request, "git-upload-pack")


@router.post("/{owner}/{repo}/git-receive-pack")
async def receive_pack(owner: str, repo: str, request: Request) -> Response:
    """The push pack transfer (``git push``)."""
    return await _proxy_pack(owner, repo, request, "git-receive-pack")


async def _proxy_pack(owner: str, repo: str, request: Request, service: str) -> Response:
    """Shared POST handler: authenticate the ticket, then stream the pack upstream."""
    claims = _ticket_from_basic_auth(request)
    if claims is None:
        return _unauthorized()
    return await proxy_git(
        claims=claims,
        owner=owner,
        repo=repo,
        git_path=service,
        service=service,
        method="POST",
        request_headers=dict(request.headers),
        request_body=request.stream(),
    )


__all__ = ["router"]
