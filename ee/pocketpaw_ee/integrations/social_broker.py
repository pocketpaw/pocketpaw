# ee/pocketpaw_ee/integrations/social_broker.py: the platform OAuth broker behind
# the built-in ``social_login`` integration (Google / GitHub sign-in for visitors of
# project sites that use better-auth).
#
#   consent   GET  /integrations/social/{provider}/authorize?site_id&return_to&nonce&next
#   decide    POST /integrations/social/{provider}/authorize   form t + action
#   callback  GET  /integrations/social/{provider}/callback?code&state
#   redeem    POST /integrations/social/redeem  Bearer PAW_GATEWAY_TOKEN, {site_id, ticket, nonce}
#
# The site Worker's ``/api/paw-auth/start`` sets a ``__Host-paw_social=<nonce>``
# cookie on the site's origin and sends the browser to ``consent``. That checks the
# site has ``social_login`` bound with this provider on, the platform client is
# configured, ``return_to`` is exactly ``https://<one of the site's own
# hosts>/api/paw-auth/callback`` (the site url's host or a LIVE custom domain; https,
# no userinfo, no port, no query; ``settings._url_host``), ``next`` is a same-origin
# path and the nonce is well formed, then shows a page naming the site.
#
# CONSENT. GitHub (and Google, once approved) skip their own consent for an app the
# visitor already approved, and every site shares the platform app, so without this
# page any site could bounce a visitor through unseen. The page holds a single-use
# token (``_oauth_state``, namespace ``social_consent``) and sets a SameSite=Strict
# cookie with its hash: only a POST from our own page in the same browser can spend
# it (a cross-site auto-submit carries no Strict cookie). Nonce CSP,
# ``frame-ancestors 'none'`` and X-Frame-Options stop clickjacking. Cancel returns to
# ``return_to`` with ``error=cancelled``. Continue stores the flow state (namespace
# ``social_broker``, 10 min, PKCE S256 for both providers, a Google id_token nonce)
# and redirects to the provider with the PLATFORM client and one fixed callback.
#
# ``callback`` trusts only that state. It exchanges the code, reads the profile
# (Google: the signed id_token; GitHub: /user plus the primary VERIFIED address from
# /user/emails, never /user's email), swaps the provider's account id for a PAIRWISE
# one (HMAC-SHA256(POCKETPAW_SOCIAL_PAIRWISE_SECRET, "site_id:provider:raw id"); the
# raw id is never stored or returned, so a visitor can't be linked across sites),
# parks the profile under an opaque single-use ticket (namespace ``social_ticket``,
# 60 s) bound to site_id + nonce, and redirects to ``return_to?ticket&next``.
#
# ``redeem`` is server to server: the site's PAW_GATEWAY_TOKEN (constant time) plus
# the nonce from the visitor's cookie. The ticket is spent on the first try, right or
# wrong; a site or nonce mismatch is refused, which is what defeats a login-CSRF link.
#
# Rate limits use the gateway's Mongo per-minute counters (``gateway._bump``, shared
# across workers): per IP on consent, decide and callback; per site on redeem, before
# the token is decrypted. Not built on better-auth's ``oAuthProxy`` plugin.

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.integrations import bindings, oauth
from pocketpaw_ee.integrations.providers import social_login
from pocketpaw_ee.integrations.settings import _url_host

logger = logging.getLogger(__name__)

PROVIDER_ID = "social_login"
CONSENT_NAMESPACE = "social_consent"
STATE_NAMESPACE = "social_broker"
TICKET_NAMESPACE = "social_ticket"
STATE_TTL_SECONDS = 600
TICKET_TTL_SECONDS = 60
CONSENT_COOKIE = "paw_social_consent"
COOKIE_PATH = "/api/v1/integrations/social/"
CALLBACK_PATH = "/api/paw-auth/callback"
MAX_NEXT = 512
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TIMEOUT = 10.0
# Per-minute caps on the shared counters: a person needs a couple, a script is cut off.
IP_PER_MIN = 30
REDEEM_SITE_PER_MIN = 300

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"
SCOPES = {"google": "openid email profile", "github": "read:user user:email"}
LABELS = {"google": "Google", "github": "GitHub"}

STATUS = {
    "invalid_request": 400,
    "invalid_ticket": 400,
    "unauthorized": 401,
    "return_to_denied": 403,
    "not_enabled": 403,
    "not_found": 404,
    "provider_unavailable": 409,
    "rate_limited": 429,
}
MESSAGES = {
    "invalid_request": "That sign-in link isn't valid.",
    "invalid_ticket": "That sign-in has expired. Try again.",
    "unauthorized": "This site isn't allowed to do that.",
    "return_to_denied": "That address isn't one of this site's own.",
    "not_enabled": "That sign-in option isn't turned on for this site.",
    "not_found": "Unknown sign-in provider.",
    "provider_unavailable": "That sign-in option isn't available right now.",
    "rate_limited": "Too many sign-in attempts. Wait a moment and try again.",
}

# Tests swap in an ``httpx.MockTransport``; None means the real network.
_transport: httpx.AsyncBaseTransport | None = None


class BrokerError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def error_response(code: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": MESSAGES[code]}},
        status_code=STATUS[code],
        headers={"Cache-Control": "no-store"},
    )


def redirect_uri(provider: str) -> str:
    return f"{oauth.callback_base()}/api/v1/integrations/social/{provider}/callback"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pairwise_id(site_id: str, provider: str, raw_id: str) -> str:
    """The id a site sees for one visitor: stable per (site, provider), unlinkable
    across sites without the platform secret."""
    key = social_login.pairwise_secret().encode("utf-8")
    return hmac.new(key, f"{site_id}:{provider}:{raw_id}".encode(), hashlib.sha256).hexdigest()


async def _limit(scope: str, key: str, cap: int) -> None:
    from pocketpaw_ee.integrations.gateway import _bump

    if await _bump(scope, key) > cap:
        raise BrokerError("rate_limited")


async def _limit_ip(request: Request) -> None:
    from pocketpaw_ee.cloud._core.rate_limit import client_ip
    from pocketpaw_ee.integrations.gateway import _rate_key

    await _limit("social_ip", _rate_key(client_ip(request)), IP_PER_MIN)


def safe_next(value: str | None) -> str | None:
    """A same-origin path ("/x?y"), "/" for empty, None when it could leave the site."""
    if not value:
        return "/"
    if not isinstance(value, str) or len(value) > MAX_NEXT or not value.startswith("/"):
        return None
    if value.startswith("//") or "\\" in value:
        return None
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    if urlsplit(value).netloc:
        return None
    return value


def site_hosts(site: Any) -> set[str]:
    """The hosts the site itself serves: its url's host and every LIVE custom domain."""
    hosts = set()
    if site.url and (host := _url_host(site.url if "://" in site.url else f"https://{site.url}")):
        hosts.add(host)
    for domain in site.domains or []:
        name = (domain.hostname or "").strip().lower().rstrip(".")
        if name and domain.status == "live":
            hosts.add(name)
    return hosts


def check_return_to(site: Any, return_to: str) -> str:
    host = _url_host(return_to or "")
    if host is None:
        raise BrokerError("return_to_denied")
    parts = urlsplit(return_to)
    if parts.scheme != "https" or parts.query or parts.fragment or parts.path != CALLBACK_PATH:
        raise BrokerError("return_to_denied")
    if host not in site_hosts(site):
        raise BrokerError("return_to_denied")
    return f"https://{host}{CALLBACK_PATH}"


async def _site_with_binding(site_id: str, provider: str) -> Any:
    from pocketpaw_ee.integrations.gateway import _site_by_id

    site = await _site_by_id(site_id)
    if site is None or site.revoked or not site.pocket_id:
        raise BrokerError("not_enabled")
    binding = await bindings.binding_for_site(site, PROVIDER_ID)
    if binding is None or provider not in social_login.enabled_providers(binding.settings):
        raise BrokerError("not_enabled")
    return site


def _provider_url(provider: str, state: str, challenge: str, oidc_nonce: str) -> str:
    params = {
        "client_id": social_login.client_id(provider),
        "redirect_uri": redirect_uri(provider),
        "scope": SCOPES[provider],
        "state": state,
        # PKCE S256 on both. GitHub OAuth Apps support it too:
        # https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider == "google":
        params.update(response_type="code", prompt="select_account", nonce=oidc_nonce)
        return f"{GOOGLE_AUTHORIZE}?{urlencode(params)}"
    params["allow_signup"] = "true"
    return f"{GITHUB_AUTHORIZE}?{urlencode(params)}"


def _back(return_to: str, **params: str) -> RedirectResponse:
    return RedirectResponse(
        f"{return_to}?{urlencode(params)}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


# ------------------------------------------------------------------ consent


def consent_html(provider: str, host: str, token: str, nonce: str) -> str:
    label, site = html.escape(LABELS[provider]), html.escape(host)
    action = html.escape(f"/api/v1/integrations/social/{provider}/authorize")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to {site}</title>
<style nonce="{nonce}">
body{{font:16px/1.5 system-ui,sans-serif;margin:0;min-height:100vh;display:grid;
place-items:center;background:#f6f6f4;color:#1b1b1a}}
main{{max-width:24rem;padding:2rem;background:#fff;border-radius:12px;box-shadow:0 1px 3px #0002}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{margin:0 0 1.5rem;color:#555}}
form{{display:flex;gap:.75rem}}
button{{flex:1;font:inherit;padding:.6rem;border-radius:8px;border:1px solid #ccc;
background:#fff;cursor:pointer}}
button[value=continue]{{background:#1b1b1a;color:#fff;border-color:#1b1b1a}}
</style></head>
<body><main>
<h1>Sign in to {site} with {label}?</h1>
<p>{site} will get your name, email address and profile picture from {label}.</p>
<form method="post" action="{action}">
<input type="hidden" name="t" value="{html.escape(token)}">
<button type="submit" name="action" value="cancel">Cancel</button>
<button type="submit" name="action" value="continue">Continue</button>
</form>
</main></body></html>"""


async def authorize(
    request: Request, provider: str, site_id: str, return_to: str, nonce: str, next_path: str
) -> Response:
    """GET: validate, then show the page that names the site. Nothing goes to the
    provider until the visitor presses Continue."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    try:
        if provider not in social_login.SOCIAL_PROVIDERS:
            raise BrokerError("not_found")
        await _limit_ip(request)
        if not _NONCE_RE.match(nonce or ""):
            raise BrokerError("invalid_request")
        nxt = safe_next(next_path)
        if nxt is None:
            raise BrokerError("invalid_request")
        if not social_login.configured(provider):
            raise BrokerError("provider_unavailable")
        site = await _site_with_binding(site_id, provider)
        back = check_return_to(site, return_to)
    except BrokerError as exc:
        return error_response(exc.code)

    token = await _oauth_state.issue(
        CONSENT_NAMESPACE,
        {
            "provider": provider,
            "site_id": str(site.id),
            "return_to": back,
            "nonce": nonce,
            "next": nxt,
        },
        ttl_seconds=STATE_TTL_SECONDS,
    )
    csp_nonce = secrets.token_urlsafe(16)
    resp = HTMLResponse(
        consent_html(provider, urlsplit(back).hostname or "", token, csp_nonce),
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'nonce-{csp_nonce}'; base-uri 'none'; "
                "frame-ancestors 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
    resp.set_cookie(
        CONSENT_COOKIE,
        _hash(token),
        max_age=STATE_TTL_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return resp


async def decide(request: Request, provider: str) -> Response:
    """POST from the consent page: Continue -> provider, Cancel -> back to the site."""
    from pocketpaw_ee.cloud.auth import _oauth_state

    try:
        await _limit_ip(request)
    except BrokerError as exc:
        return error_response(exc.code)
    form = await request.form()
    token, action = str(form.get("t") or ""), str(form.get("action") or "")
    cookie = request.cookies.get(CONSENT_COOKIE) or ""
    # The Strict cookie proves the POST came from our own page in this browser.
    if not token or not cookie or not hmac.compare_digest(cookie, _hash(token)):
        return error_response("invalid_request")
    try:
        payload = await _oauth_state.consume(CONSENT_NAMESPACE, token)
    except CloudError:
        return error_response("invalid_ticket")
    if payload.get("provider") != provider or action not in ("continue", "cancel"):
        return error_response("invalid_request")
    resp: Response
    if action == "cancel":
        resp = _back(str(payload["return_to"]), error="cancelled", next=str(payload["next"]))
    else:
        verifier, challenge = _oauth_state.pkce_pair()
        oidc_nonce = _oauth_state.new_nonce()
        state = await _oauth_state.issue(
            STATE_NAMESPACE,
            {**payload, "code_verifier": verifier, "oidc_nonce": oidc_nonce},
            ttl_seconds=STATE_TTL_SECONDS,
        )
        resp = RedirectResponse(
            _provider_url(provider, state, challenge, oidc_nonce),
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    resp.delete_cookie(
        CONSENT_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict"
    )
    return resp


# ----------------------------------------------------------------- profiles


def google_profile(claims: dict[str, Any]) -> dict[str, Any]:
    """Profile from VERIFIED id_token claims, with the RAW ``sub`` (``callback`` makes
    it pairwise). ``email_verified`` only when the claim is a real ``true``."""
    sub = claims.get("sub")
    if not sub:
        raise BrokerError("provider_error")
    email = claims.get("email")
    return {
        "provider": "google",
        "provider_account_id": str(sub),
        "email": email.lower() if isinstance(email, str) and email else None,
        "email_verified": claims.get("email_verified") is True,
        "name": str(claims.get("name") or ""),
        "avatar_url": str(claims.get("picture") or ""),
    }


async def _google(code: str, verifier: str, oidc_nonce: str) -> dict[str, Any]:
    from pocketpaw_ee.cloud.auth.sso import oidc

    discovery = await oidc.discover(GOOGLE_ISSUER, "google")
    tokens = await oidc.exchange_code(
        discovery["token_endpoint"],
        code,
        social_login.client_id("google"),
        social_login.client_secret("google"),
        redirect_uri("google"),
        code_verifier=verifier,
    )
    id_token = tokens.get("id_token")
    if not id_token:
        raise BrokerError("provider_error")
    claims = await oidc.parse_id_token(
        id_token,
        discovery["jwks_uri"],
        audience=social_login.client_id("google"),
        issuer=GOOGLE_ISSUER,
        nonce=oidc_nonce,
    )
    return google_profile(claims)


async def _github(code: str, verifier: str) -> dict[str, Any]:
    from pocketpaw_ee.cloud.auth.social.providers.github import pick_verified_email

    async with httpx.AsyncClient(timeout=_TIMEOUT, transport=_transport) as client:
        resp = await client.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": social_login.client_id("github"),
                "client_secret": social_login.client_secret("github"),
                "code": code,
                "redirect_uri": redirect_uri("github"),
                "code_verifier": verifier,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token or body.get("error"):
            raise BrokerError("provider_error")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        user = await client.get(f"{GITHUB_API}/user", headers=headers)
        user.raise_for_status()
        profile = user.json()
        emails = await client.get(f"{GITHUB_API}/user/emails", headers=headers)
        rows = emails.json() if emails.status_code < 400 else []
    if not isinstance(profile, dict) or profile.get("id") is None:
        raise BrokerError("provider_error")
    # Only /user/emails vouches for an address; /user's ``email`` is self-asserted.
    email = pick_verified_email(rows if isinstance(rows, list) else [])
    return {
        "provider": "github",
        "provider_account_id": str(profile["id"]),
        "email": email,
        "email_verified": email is not None,
        "name": str(profile.get("name") or profile.get("login") or ""),
        "avatar_url": str(profile.get("avatar_url") or ""),
    }


async def fetch_profile(provider: str, code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The provider's profile with its RAW account id (never leaves this module)."""
    verifier = payload.get("code_verifier", "")
    if provider == "google":
        return await _google(code, verifier, payload.get("oidc_nonce", ""))
    return await _github(code, verifier)


# ------------------------------------------------------------------ callback


async def callback(request: Request, provider: str, code: str, state: str) -> Response:
    from pocketpaw_ee.cloud.auth import _oauth_state

    try:
        await _limit_ip(request)
    except BrokerError as exc:
        return error_response(exc.code)
    try:
        payload = await _oauth_state.consume(STATE_NAMESPACE, state or "")
    except CloudError:
        return error_response("invalid_ticket")
    if payload.get("provider") != provider:
        return error_response("invalid_ticket")
    return_to, nxt = str(payload["return_to"]), str(payload.get("next") or "/")
    if not code:
        return _back(return_to, error="access_denied", next=nxt)
    try:
        profile = await fetch_profile(provider, code, payload)
        profile["provider_account_id"] = pairwise_id(
            str(payload["site_id"]), provider, str(profile["provider_account_id"])
        )
    except Exception as exc:  # noqa: BLE001 - every failure is one safe answer
        logger.warning("social broker: %s callback failed: %s", provider, type(exc).__name__)
        return _back(return_to, error="provider_error", next=nxt)
    ticket = await _oauth_state.issue(
        TICKET_NAMESPACE,
        {"site_id": payload["site_id"], "nonce": payload["nonce"], "profile": profile},
        ttl_seconds=TICKET_TTL_SECONDS,
    )
    return _back(return_to, ticket=ticket, next=nxt)


# -------------------------------------------------------------------- redeem


async def redeem(request: Request) -> JSONResponse:
    """``{site_id, ticket, nonce}`` + the site's gateway token -> ``{ok, result: profile}``."""
    from pocketpaw_ee.cloud.auth import _oauth_state
    from pocketpaw_ee.integrations.gateway import _site_by_id
    from pocketpaw_ee.sites import site_secrets

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return error_response("invalid_request")
    site_id, ticket, nonce = (body.get(k) for k in ("site_id", "ticket", "nonce"))
    if not all(isinstance(v, str) and v and len(v) <= 256 for v in (site_id, ticket, nonce)):
        return error_response("invalid_request")

    auth = request.headers.get("authorization", "")
    site = await _site_by_id(site_id)
    if site is None or site.revoked or not site.pocket_id or not auth.lower().startswith("bearer "):
        return error_response("unauthorized")
    try:
        await _limit("social_redeem", f"{site.workspace}:{site.id}", REDEEM_SITE_PER_MIN)
    except BrokerError as exc:
        return error_response(exc.code)
    if not await site_secrets.secret_matches(
        workspace_id=site.workspace,
        pocket_id=site.pocket_id,
        name=bindings.GATEWAY_TOKEN_NAME,
        candidate=auth[7:].strip(),
    ):
        return error_response("unauthorized")

    try:
        payload = await _oauth_state.consume(TICKET_NAMESPACE, ticket)
    except CloudError:
        return error_response("invalid_ticket")
    if payload.get("site_id") != str(site.id) or not hmac.compare_digest(
        str(payload.get("nonce", "")).encode(), nonce.encode()
    ):
        return error_response("invalid_ticket")
    profile = payload.get("profile") or {}
    try:
        await _site_with_binding(str(site.id), str(profile.get("provider", "")))
    except BrokerError as exc:
        return error_response(exc.code)
    return JSONResponse({"ok": True, "result": profile}, headers={"Cache-Control": "no-store"})
