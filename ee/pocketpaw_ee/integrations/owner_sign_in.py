# ee/pocketpaw_ee/integrations/owner_sign_in.py: "Open owner page", the only way a
# person becomes an owner or staff account on a generated site's own better-auth.
#
#   issue   POST /sites/by-pocket/{pocket_id}/owner-sign-in  {next?}  Paw session
#           -> {url, role, expires_in}
#   redeem  POST /integrations/owner-sign-in/redeem  Bearer PAW_GATEWAY_TOKEN
#           {site_id, ticket} -> {ok, result: {account_id, email, email_verified, name, role}}
#   check   POST /integrations/owner-sign-in/check   Bearer PAW_GATEWAY_TOKEN
#           {site_id, account_id} -> {ok, result: {role}}   (role is null when gone)
#   peek    POST /integrations/owner-sign-in/peek    Bearer PAW_GATEWAY_TOKEN
#           {site_id, ticket} -> {ok, result: {name, email, role}}   (does NOT spend it)
#
# WHO: the pocket owner is "owner"; people on the pocket's team (pocket.team) are
# "staff". A shared_with share stores no access level (a view share looks like an edit
# share), so it gives no site role until levels are stored. A workspace member who can
# merely see a workspace-visible pocket gets nothing. Every person must also be an
# ACTIVE, VERIFIED user who is still a member of the site's workspace, checked at issue
# and on every ``check``: removing someone from the workspace, deactivating them or
# un-verifying them ends their site role even if a pocket still lists them. The Paw
# email must be verified because the site links an existing account by that email (and
# strips its other ways in). Anyone who can edit the site can also change its code, so
# the people given a role are trusted with it. The site re-asks ``check`` every few
# minutes (paw-sites paw-owner-auth.ts), so a lost role ends within that window.
#
# The link is ``https://<site host>/api/paw-owner/callback#ticket=...&next=...`` (a live
# custom domain first, else the site's url). The ticket rides in the FRAGMENT: browsers
# never send it, so no request log (ours, Cloudflare's Workers Logs) and no Referer ever
# carries it. A tiny page on the site ``peek``s it to show "Continue as <name> (<email>)"
# and redeems it only when the person presses Continue, so a crafted link cannot sign
# someone in to a stranger's account without them seeing whose it is.
#
# THE TICKET (threat model in paw-sites recipes/README.md, "Owner sign-in"):
#   * opaque, 32 random bytes, stored server-side (``_oauth_state``, namespace
#     ``site_owner_ticket``): nothing in it can be forged or edited, only referenced
#   * single use: read-and-delete is one atomic step, spent on the first redeem that
#     passes the token check, right or wrong site
#   * 60 seconds to be redeemed (the site session it starts has its own 12 h cap)
#   * bound to the site id at issue and redeemable only with that site's own
#     PAW_GATEWAY_TOKEN, compared in constant time
#   * never logged here; issue is audited (actor, site, role), never the ticket
# ``account_id`` is a pairwise id, HMAC-SHA256 over "site_id:user_id" keyed from the
# deployment encryption key: stable per (site, person), unlinkable across sites.
#
# Gateway-token auth checks the token BEFORE any per-site bucket is spent, answers a
# missing site and a wrong token identically, and caps failures per (IP, site).

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

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.integrations import bindings
from pocketpaw_ee.integrations.settings import _url_host
from pocketpaw_ee.integrations.social_broker import safe_next

logger = logging.getLogger(__name__)

TICKET_NAMESPACE = "site_owner_ticket"
TICKET_TTL_SECONDS = 60
CALLBACK_PATH = "/api/paw-owner/callback"
ISSUE_USER_PER_MIN = 20
REDEEM_SITE_PER_MIN = 120
CHECK_SITE_PER_MIN = 600
AUTH_FAIL_PER_MIN = 10
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


def _member_ids(members: Any) -> list[str]:
    return [str(getattr(m, "id", m)) for m in members or [] if m]


def role_of(pocket: Any, user_id: str) -> str | None:
    """ "owner" for the pocket owner, "staff" for its team, else None. shared_with gives
    nothing: it stores no access level, so a view share would otherwise become staff."""
    if not user_id:
        return None
    if pocket.owner == user_id:
        return ROLE_OWNER
    if user_id in _member_ids(pocket.team):
        return ROLE_STAFF
    return None


def in_good_standing(user: Any, workspace_id: str) -> bool:
    """An active, verified user who is still a member of ``workspace_id``."""
    if user is None or getattr(user, "is_active", False) is not True:
        return False
    if getattr(user, "is_verified", False) is not True:
        return False
    return any(
        str(getattr(m, "workspace", "")) == workspace_id
        for m in getattr(user, "workspaces", None) or []
    )


async def _load_user(user_id: str) -> Any:
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User

    try:
        return await User.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 - a malformed id is a missing user
        return None


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


async def _within(scope: str, key: str, cap: int) -> bool:
    from pocketpaw_ee.integrations.gateway import _bump

    return await _bump(scope, key) <= cap


def _audit(user_id: str, workspace_id: str, pocket_id: str, site_id: str, role: str) -> None:
    try:
        from pocketpaw_ee.guards import audit

        audit.log_privileged_action(
            actor=user_id,
            action="sites.owner_sign_in.issue",
            resource_id=pocket_id,
            workspace_id=workspace_id,
            site_id=site_id,
            role=role,
        )
    except Exception:  # noqa: BLE001
        logger.debug("owner_sign_in: audit failed", exc_info=True)


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
    # A pocket in this workspace the caller can at least see (404 outside it).
    pocket = await site_secrets._require_edit(workspace_id, user_id, pocket_id)
    role = role_of(pocket, user_id)
    if role is None:
        raise Forbidden(
            "sites.owner_page_not_collaborator",
            "Only the site's owner and the people they shared it with can open its owner page.",
        )
    if getattr(user, "is_verified", False) is not True:
        raise ValidationError(
            "sites.owner_page_unverified",
            "Verify your email in Paw first, then open the owner page.",
        )
    if not in_good_standing(user, workspace_id):
        raise Forbidden(
            "sites.owner_page_not_collaborator",
            "Only the site's owner and the people they shared it with can open its owner page.",
        )
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
    if not await _within("owner_issue", f"{workspace_id}:{user_id}", ISSUE_USER_PER_MIN):
        raise RateLimited(
            "sites.owner_page_rate_limited", "Too many owner links. Wait a moment and try again."
        )
    # The Worker redeems with its gateway token. A site that never had one gets it now,
    # but its live Worker only holds it after the next publish: say so plainly.
    if await site_secrets.ensure_generated_secret(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        name=bindings.GATEWAY_TOKEN_NAME,
        description="Created by Paw for Sites integrations. Server-side calls only.",
    ):
        raise ConflictError(
            "sites.owner_page_publish_needed",
            "Publish the site once to turn on owner sign-in, then open the owner page.",
        )
    site_id = str(site.id)
    ticket = await _oauth_state.issue(
        TICKET_NAMESPACE,
        {
            "site_id": site_id,
            "account_id": account_id(site_id, user_id),
            "email": email,
            "email_verified": True,
            "name": str(getattr(user, "full_name", "") or "").strip(),
            "role": role,
        },
        ttl_seconds=TICKET_TTL_SECONDS,
    )
    _audit(user_id, workspace_id, pocket_id, site_id, role)
    fragment = urlencode({"ticket": ticket, "next": nxt})
    return OwnerSignInLink(
        url=f"https://{host}{CALLBACK_PATH}#{fragment}", role=role, expires_in=TICKET_TTL_SECONDS
    )


async def _body(request: Request, *fields: str) -> dict[str, str] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    values = {f: body.get(f) for f in fields}
    if not all(isinstance(v, str) and v and len(v) <= 256 for v in values.values()):
        return None
    return values  # type: ignore[return-value]


async def _authenticated_site(request: Request, site_id: str) -> tuple[Any, JSONResponse | None]:
    """The site whose PAW_GATEWAY_TOKEN the request carries, else a refusal. A missing
    site and a wrong token look the same; failures are capped per (IP, site)."""
    from pocketpaw_ee.cloud._core.rate_limit import client_ip
    from pocketpaw_ee.integrations.gateway import _rate_key, _site_by_id
    from pocketpaw_ee.sites import site_secrets

    auth = request.headers.get("authorization", "")
    candidate = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    site = await _site_by_id(site_id)
    usable = site is not None and not site.revoked and bool(site.pocket_id)
    if usable and candidate:
        ok = await site_secrets.secret_matches(
            workspace_id=site.workspace,
            pocket_id=site.pocket_id,
            name=bindings.GATEWAY_TOKEN_NAME,
            candidate=candidate,
        )
    else:
        hmac.compare_digest(candidate.encode(), b"\x00" * 43)  # same work, same answer
        ok = False
    if ok:
        return site, None
    key = f"{_rate_key(client_ip(request))}:{site_id[:64]}"
    if not await _within("owner_auth_fail", key, AUTH_FAIL_PER_MIN):
        return None, _error("rate_limited")
    return None, _error("unauthorized")


async def redeem(request: Request) -> JSONResponse:
    """``{site_id, ticket}`` + the site's gateway token -> the owner identity, once."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    body = await _body(request, "site_id", "ticket")
    if body is None:
        return _error("invalid_request")
    site, refused = await _authenticated_site(request, body["site_id"])
    if refused is not None:
        return refused
    if not await _within("owner_redeem", f"{site.workspace}:{site.id}", REDEEM_SITE_PER_MIN):
        return _error("rate_limited")
    try:
        payload = await _oauth_state.consume(TICKET_NAMESPACE, body["ticket"])
    except CloudError:
        return _error("invalid_ticket")
    if not hmac.compare_digest(str(payload.get("site_id", "")).encode(), str(site.id).encode()):
        logger.warning("owner_sign_in: site %s presented another site's ticket", site.id)
        return _error("invalid_ticket")
    result: dict[str, Any] = {
        k: str(payload.get(k) or "") for k in ("account_id", "email", "name", "role")
    }
    if result["role"] not in (ROLE_OWNER, ROLE_STAFF) or not result["account_id"]:
        return _error("invalid_ticket")
    result["email_verified"] = payload.get("email_verified") is True
    logger.info("owner_sign_in: %s signed in on site %s", result["role"], site.id)
    return JSONResponse({"ok": True, "result": result}, headers={"Cache-Control": "no-store"})


async def check(request: Request) -> JSONResponse:
    """``{site_id, account_id}`` + the site's gateway token -> that person's role now,
    or null when they are no longer the owner or a collaborator."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.pocket import Pocket

    body = await _body(request, "site_id", "account_id")
    if body is None:
        return _error("invalid_request")
    site, refused = await _authenticated_site(request, body["site_id"])
    if refused is not None:
        return refused
    if not await _within("owner_check", f"{site.workspace}:{site.id}", CHECK_SITE_PER_MIN):
        return _error("rate_limited")
    role: str | None = None
    try:
        pocket = await Pocket.get(PydanticObjectId(site.pocket_id))
    except Exception:  # noqa: BLE001 - a malformed id is a missing pocket
        pocket = None
    if pocket is not None and pocket.workspace == site.workspace:
        people = [pocket.owner, *_member_ids(pocket.team)]
        for user_id in dict.fromkeys(p for p in people if p):
            if hmac.compare_digest(account_id(str(site.id), user_id), body["account_id"]):
                if in_good_standing(await _load_user(user_id), site.workspace):
                    role = role_of(pocket, user_id)
                break
    return JSONResponse(
        {"ok": True, "result": {"role": role}}, headers={"Cache-Control": "no-store"}
    )


async def peek(request: Request) -> JSONResponse:
    """``{site_id, ticket}`` + the site's gateway token -> who the ticket would sign in
    (name, email, role), WITHOUT spending it: the site shows "Continue as ..." first."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    body = await _body(request, "site_id", "ticket")
    if body is None:
        return _error("invalid_request")
    site, refused = await _authenticated_site(request, body["site_id"])
    if refused is not None:
        return refused
    if not await _within("owner_check", f"{site.workspace}:{site.id}", CHECK_SITE_PER_MIN):
        return _error("rate_limited")
    payload = await _oauth_state.peek(TICKET_NAMESPACE, body["ticket"])
    if payload is None or not hmac.compare_digest(
        str(payload.get("site_id", "")).encode(), str(site.id).encode()
    ):
        return _error("invalid_ticket")
    result = {k: str(payload.get(k) or "") for k in ("name", "email", "role")}
    return JSONResponse({"ok": True, "result": result}, headers={"Cache-Control": "no-store"})
