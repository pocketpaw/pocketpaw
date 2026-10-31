# ee/pocketpaw_ee/integrations/owner_sign_in.py: "Open owner page", the only way a
# person becomes an owner or staff account on a generated site's own better-auth.
#
#   issue   POST /sites/by-pocket/{pocket_id}/owner-sign-in  {next?}  Paw session
#           -> {url, role, expires_in}
#   redeem  POST /integrations/owner-sign-in/redeem  Bearer PAW_GATEWAY_TOKEN
#           {site_id, ticket} -> {ok, result: {account_id, email, name, role}}
#
# A Paw user with edit access to a PUBLISHED site asks Paw for a link. The role is
# fixed here, never by the site: "owner" for the pocket owner, "staff" for anyone else
# with edit access. The link is ``https://<site host>/api/paw-owner/callback?ticket&next``
# (a live custom domain first, else the site's url): the site's Worker trades the
# ticket server to server for the identity above and signs that person in (paw-sites
# better-auth recipe, src/server/auth/paw-owner*.ts). Before this, generated sites let
# the first visitor to /sign-in claim the only owner account.
#
# THE TICKET (threat model in paw-sites recipes/README.md, "Owner sign-in"):
#   * opaque, 32 random bytes, stored server-side (``_oauth_state``, namespace
#     ``site_owner_ticket``): nothing in it can be forged or edited, only referenced
#   * single use: read-and-delete is one atomic step, spent on the first redeem that
#     passes the token check, right or wrong site
#   * 60 seconds: the browser follows the link at once; a ticket leaked later (history,
#     a platform request log) is already spent or expired
#   * bound to the site id at issue and redeemable only with that site's own
#     PAW_GATEWAY_TOKEN (a per-site random secret, compared in constant time), so a
#     ticket is worthless on any other site or without the Worker
#   * never logged, here or in the Worker; the callback answers with a 302 carrying
#     ``Referrer-Policy: no-referrer`` and ``Cache-Control: no-store``
# ``account_id`` is a pairwise id, HMAC-SHA256 over "site_id:user_id" keyed from the
# deployment encryption key: stable per (site, person), unlinkable across sites, and
# the raw Paw user id never reaches the site.
#
# Rate limits use the gateway's shared per-minute counters: per user on issue, per
# site on redeem (before the token is decrypted).

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, RateLimited, ValidationError
from pocketpaw_ee.integrations import bindings
from pocketpaw_ee.integrations.settings import _url_host
from pocketpaw_ee.integrations.social_broker import safe_next

logger = logging.getLogger(__name__)

TICKET_NAMESPACE = "site_owner_ticket"
TICKET_TTL_SECONDS = 60
CALLBACK_PATH = "/api/paw-owner/callback"
ISSUE_USER_PER_MIN = 20
REDEEM_SITE_PER_MIN = 120
ROLE_OWNER = "owner"
ROLE_STAFF = "staff"

STATUS = {"invalid_request": 400, "invalid_ticket": 400, "unauthorized": 401, "rate_limited": 429}
MESSAGES = {
    "invalid_request": "That owner sign-in isn't valid.",
    "invalid_ticket": "That owner link has expired. Open it again from Paw.",
    "unauthorized": "This site isn't allowed to do that.",
    "rate_limited": "Too many sign-in attempts. Wait a moment and try again.",
}


class OwnerSignInBody(BaseModel):
    next: str = "/"


class OwnerSignInLink(BaseModel):
    url: str
    role: str
    expires_in: int


def _error(code: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": MESSAGES[code]}},
        status_code=STATUS[code],
        headers={"Cache-Control": "no-store"},
    )


def account_id(site_id: str, user_id: str) -> str:
    """The id the site stores for one person: stable per (site, person), unlinkable
    across sites, never the raw Paw user id."""
    raw = os.environ.get("CLOUD_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise ConflictError(
            "sites.owner_page_unavailable", "Owner sign-in isn't set up on this server."
        )
    key = hashlib.sha256(b"paw-site-owner-account\x00" + raw.encode()).digest()
    return hmac.new(key, f"{site_id}:{user_id}".encode(), hashlib.sha256).hexdigest()


def owner_host(site: Any) -> str | None:
    """Where the owner page opens: a LIVE custom domain first, else the site's url."""
    for domain in getattr(site, "domains", None) or []:
        name = (getattr(domain, "hostname", "") or "").strip().lower().rstrip(".")
        if name and getattr(domain, "status", "") == "live":
            return name
    url = (getattr(site, "url", "") or "").strip()
    if not url:
        return None
    return _url_host(url if "://" in url else f"https://{url}")


async def _limit(scope: str, key: str, cap: int) -> bool:
    from pocketpaw_ee.integrations.gateway import _bump

    return await _bump(scope, key) <= cap


async def issue(*, workspace_id: str, user: Any, pocket_id: str, next_path: str) -> OwnerSignInLink:
    """A one-time owner-page link for ``user`` on the pocket's published site."""
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.auth import _oauth_state
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites import site_secrets

    user_id = str(getattr(user, "id", "") or "")
    nxt = safe_next(next_path)
    if nxt is None:
        raise ValidationError("sites.owner_page_next", "next must be a path on the site.")
    email = str(getattr(user, "email", "") or "").strip()
    if not email:
        raise ValidationError(
            "sites.owner_page_email", "Your Paw account needs an email to open the owner page."
        )
    # Edit access to a pocket in this workspace (404 outside it, 403 without access).
    pocket = await site_secrets._require_edit(workspace_id, user_id, pocket_id)
    site = await sites_service._canonical_site_doc(workspace_id, pocket_id)
    host = owner_host(site) if site is not None and not site.revoked else None
    if site is None or host is None:
        raise ConflictError(
            "sites.owner_page_unpublished", "Publish the site first, then open its owner page."
        )
    if not crypto.is_configured():
        raise ConflictError(
            "sites.owner_page_unavailable", "Owner sign-in isn't set up on this server."
        )
    if not await _limit("owner_issue", f"{workspace_id}:{user_id}", ISSUE_USER_PER_MIN):
        raise RateLimited(
            "sites.owner_page_rate_limited", "Too many owner links. Wait a moment and try again."
        )

    # The Worker redeems with its gateway token; a site that never linked an
    # integration gets one now (its next publish binds it).
    await bindings.ensure_gateway_token(workspace_id, pocket_id)
    site_id = str(site.id)
    role = ROLE_OWNER if pocket.owner == user_id else ROLE_STAFF
    ticket = await _oauth_state.issue(
        TICKET_NAMESPACE,
        {
            "site_id": site_id,
            "account_id": account_id(site_id, user_id),
            "email": email,
            "name": str(getattr(user, "full_name", "") or "").strip(),
            "role": role,
        },
        ttl_seconds=TICKET_TTL_SECONDS,
    )
    logger.info("owner_sign_in: %s link issued for site %s", role, site_id)
    query = urlencode({"ticket": ticket, "next": nxt})
    return OwnerSignInLink(
        url=f"https://{host}{CALLBACK_PATH}?{query}", role=role, expires_in=TICKET_TTL_SECONDS
    )


async def redeem(request: Request) -> JSONResponse:
    """``{site_id, ticket}`` + the site's gateway token -> the owner identity, once."""
    from pocketpaw_ee.cloud.auth import _oauth_state
    from pocketpaw_ee.integrations.gateway import _site_by_id
    from pocketpaw_ee.sites import site_secrets

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _error("invalid_request")
    site_id, ticket = body.get("site_id"), body.get("ticket")
    if not all(isinstance(v, str) and v and len(v) <= 256 for v in (site_id, ticket)):
        return _error("invalid_request")

    auth = request.headers.get("authorization", "")
    site = await _site_by_id(site_id)
    if site is None or site.revoked or not site.pocket_id or not auth.lower().startswith("bearer "):
        return _error("unauthorized")
    if not await _limit("owner_redeem", f"{site.workspace}:{site.id}", REDEEM_SITE_PER_MIN):
        return _error("rate_limited")
    if not await site_secrets.secret_matches(
        workspace_id=site.workspace,
        pocket_id=site.pocket_id,
        name=bindings.GATEWAY_TOKEN_NAME,
        candidate=auth[7:].strip(),
    ):
        return _error("unauthorized")

    try:
        payload = await _oauth_state.consume(TICKET_NAMESPACE, ticket)
    except CloudError:
        return _error("invalid_ticket")
    if not hmac.compare_digest(str(payload.get("site_id", "")).encode(), str(site.id).encode()):
        logger.warning("owner_sign_in: site %s presented another site's ticket", site.id)
        return _error("invalid_ticket")
    result = {k: str(payload.get(k) or "") for k in ("account_id", "email", "name", "role")}
    if result["role"] not in (ROLE_OWNER, ROLE_STAFF) or not result["account_id"]:
        return _error("invalid_ticket")
    logger.info("owner_sign_in: %s signed in on site %s", result["role"], site.id)
    return JSONResponse({"ok": True, "result": result}, headers={"Cache-Control": "no-store"})
