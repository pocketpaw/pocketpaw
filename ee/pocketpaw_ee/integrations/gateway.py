# ee/pocketpaw_ee/integrations/gateway.py: the one path a published site uses.
#
#   POST /sites/{site_id}/x/{provider}/{action}   body {args: {}, turnstile_token?}
#
# Gate order (each refusal is ``{ok: false, error: {code, message}}``):
#   1. per-IP bucket (in-process)                          -> rate_limited 429
#   2. auth, one of:
#        X-Paw-Site-Key: the site's public embed key (browser). Same credential
#          chain as the concierge (``site_keys.lookup_site_by_key``: type, length,
#          revoked, constant-time), the key's site must be ``site_id``, then a
#          fail-closed origin gate over the site's allowlist + own hosts, or the
#          origin of one of the site's OWN drafts (``preview_origin``: a
#          ``<token>.<preview base>`` host whose stored token resolves to the
#          site's pocket; another pocket's draft is refused)
#                                                          -> unauthorized 401 / origin_denied 403
#        Authorization: Bearer <PAW_GATEWAY_TOKEN> (project Worker, server side),
#          compared in constant time inside ``site_secrets``  -> unauthorized 401
#   3. provider registered, not an embed (embeds have no actions), action declared
#      + public, site bound, action enabled, connect kinds have an active grant
#                                              -> not_connected 409 / action_disabled 403
#   4. Turnstile on write actions, site-key callers only. A site with the
#      ``turnstile`` integration verifies against its own widget secret and the
#      answer's hostname must be one of the site's hosts; otherwise the platform
#      secret applies when one is configured               -> turnstile_required 403
#   5. ``SiteRateCounter`` per (site, action) and per (site, action, IP hash)
#                                                          -> rate_limited 429
#   6. typed args (extra keys refused, string caps)        -> invalid_args 422
#   7. handler, with a token from ``vault.get_valid_token`` -> provider_error 502;
#      a handler's ``ActionRefused`` passes its own code through (invalid_args 422,
#      rate_limited 429, setup_required 409, the last also marks the binding)
#
# Auth, Turnstile, the rate counter and CORS are the shared primitives in
# site_gate.py (the AI API uses the same ones).
# The site-key path does NOT use ``resolve_site_key_with_site``: that resolver also
# refuses sites without a live concierge, which would switch integrations off for
# most sites. It reuses the same key lookup and ``origin_allowed`` primitives.
# A response never carries a token; handlers return safe results only.

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.integrations import bindings, site_gate, vault
from pocketpaw_ee.integrations.providers import get_provider
from pocketpaw_ee.integrations.providers.base import (
    ActionContext,
    ActionRefused,
    ProviderError,
)

logger = logging.getLogger(__name__)

STATUS = {
    "unauthorized": 401,
    "origin_denied": 403,
    "not_connected": 409,
    "action_disabled": 403,
    "turnstile_required": 403,
    "rate_limited": 429,
    "invalid_args": 422,
    "setup_required": 409,
    "provider_error": 502,
}
MESSAGES = {
    "unauthorized": "This site isn't allowed to call that.",
    "origin_denied": "This page's address isn't allowed for this site.",
    "not_connected": "This integration isn't connected for this site.",
    "action_disabled": "That action isn't turned on for this site.",
    "turnstile_required": "Please confirm you're a person and try again.",
    "rate_limited": "Too many requests. Wait a moment and try again.",
    "invalid_args": "Some of the details sent aren't valid.",
    "setup_required": "This integration needs the site owner to finish setting it up.",
    "provider_error": "The service didn't respond. Try again in a moment.",
}

MAX_BODY_BYTES = 16 * 1024
MAX_STRING = 2000
GATEWAY_TOKEN_SITE_MULTIPLIER = 10

# 120 gateway calls a minute per address across every site: a person clicking
# around needs a handful, a script gets cut off before it reaches Mongo.
_ip_limiter = RateLimiter(rate=120.0 / 60.0, capacity=120)


class GatewayError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or MESSAGES[code]
        super().__init__(code)


def error_response(code: str, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message or MESSAGES[code]}},
        status_code=STATUS[code],
    )


# The shared site gates (site_gate.py), under the names this module and its
# importers (owner_sign_in, social_broker, webhooks, _commerce) have always used.
_rate_key = site_gate.rate_key
_site_by_id = site_gate.site_by_id
_matches = site_gate.matches
_own_draft = site_gate.own_draft
_turnstile_configured = site_gate.turnstile_configured
_site_widget_shared = site_gate.site_widget
_bump = site_gate.bump


async def _authenticate(request: Request, site_id: str) -> tuple[Any, str]:
    try:
        site, caller, _preview = await site_gate.authenticate(request, site_id)
    except site_gate.SiteGateError as exc:
        raise GatewayError(exc.code) from None
    return site, caller


async def _read_body(request: Request) -> tuple[dict, str]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise GatewayError("invalid_args", "The request is too large.")
    if not raw.strip():
        return {}, ""
    try:
        body = json.loads(raw)
    except ValueError:
        raise GatewayError("invalid_args", "The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise GatewayError("invalid_args", "The request body must be a JSON object.")
    args = body.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise GatewayError("invalid_args", "`args` must be an object.")
    token = body.get("turnstile_token") or ""
    return args, token if isinstance(token, str) else ""


def _check_strings(value: Any, depth: int = 0) -> None:
    if depth > 4:
        raise GatewayError("invalid_args", "The details are nested too deeply.")
    if isinstance(value, str) and len(value) > MAX_STRING:
        raise GatewayError("invalid_args", "A value is too long.")
    if isinstance(value, dict):
        for v in value.values():
            _check_strings(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _check_strings(v, depth + 1)


async def _verify_turnstile(token: str, ip: str, widget: tuple[str, list[str]] | None = None):
    try:
        await site_gate.verify_turnstile(token, ip, widget)
    except site_gate.SiteGateError as exc:
        raise GatewayError(exc.code) from None


async def _site_widget(site: Any) -> tuple[str, list[str]] | None:
    try:
        return await _site_widget_shared(site)
    except site_gate.SiteGateError as exc:
        raise GatewayError(exc.code) from None


async def _rate_limit(site: Any, provider: str, action: str, spec: Any, caller: str, ip: str):
    ip_cap, site_cap = spec.caps()
    base = f"{site.workspace}:{site.id}:{provider}:{action}"
    if caller == "gateway_token":
        if await _bump("gw_site", base) > site_cap * GATEWAY_TOKEN_SITE_MULTIPLIER:
            raise GatewayError("rate_limited")
        return
    if await _bump("gw_ip", f"{base}:{_rate_key(ip)}") > ip_cap:
        raise GatewayError("rate_limited")
    if await _bump("gw_site", base) > site_cap:
        raise GatewayError("rate_limited")


async def call(request: Request, site_id: str, provider_id: str, action: str) -> JSONResponse:
    try:
        result = await _call(request, site_id, provider_id, action)
    except GatewayError as exc:
        return error_response(exc.code, exc.message)
    return JSONResponse({"ok": True, "result": result})


async def _call(request: Request, site_id: str, provider_id: str, action: str) -> dict:
    from pocketpaw_ee.cloud._core.rate_limit import client_ip

    ip = client_ip(request)
    if not _ip_limiter.check(f"integrations-gw:{ip}").allowed:
        raise GatewayError("rate_limited")

    site, caller = await _authenticate(request, site_id)

    provider = get_provider(provider_id)
    if provider is None:
        raise GatewayError("not_connected")
    if provider.kind == "embed":
        # An embed is markup in the page; it has nothing to call.
        raise GatewayError("action_disabled")
    spec = provider.actions.get(action)
    if spec is None or not spec.public:
        raise GatewayError("action_disabled")
    binding = await bindings.binding_for_site(site, provider.id)
    if binding is None:
        raise GatewayError("not_connected")
    if action not in (binding.enabled_actions or []):
        raise GatewayError("action_disabled")
    conn = None
    if provider.kind == "connect":
        conn = await bindings.connection_for(binding)
        if conn is None or conn.status != "active":
            raise GatewayError("not_connected")

    args, turnstile_token = await _read_body(request)
    if spec.write and caller == "site_key":
        widget = await _site_widget(site)
        if widget is not None:
            await _verify_turnstile(turnstile_token, ip, widget)
        elif _turnstile_configured():
            await _verify_turnstile(turnstile_token, ip)

    await _rate_limit(site, provider.id, action, spec, caller, ip)

    _check_strings(args)
    try:
        parsed = spec.args.model_validate(args)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "args"
        raise GatewayError("invalid_args", f"`{where}` is not valid.") from None

    async def _token(rejected: str = "") -> str:
        return await vault.get_valid_token(conn, rejected=rejected)

    ctx = ActionContext(
        workspace_id=site.workspace,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        provider=provider.id,
        action=action,
        settings=dict(binding.settings or {}),
        caller=caller,  # type: ignore[arg-type]
        get_token=_token if conn is not None else None,
        connection_meta=dict(getattr(conn, "meta", None) or {}) if conn is not None else {},
    )
    try:
        result = await spec.handler(ctx, parsed)
    except vault.NotConnected:
        raise GatewayError("not_connected") from None
    except ActionRefused as exc:
        logger.info("integrations: %s/%s refused: %s", provider.id, action, exc.code)
        if exc.code == "setup_required":
            await bindings.mark_setup_error(binding, exc.setup_error)
        raise GatewayError(exc.code, exc.message or None) from None
    except ProviderError as exc:
        logger.info("integrations: %s/%s provider error: %s", provider.id, action, exc)
        raise GatewayError("provider_error") from None
    except Exception:
        logger.exception("integrations: %s/%s handler crashed", provider.id, action)
        raise GatewayError("provider_error") from None
    return result if isinstance(result, dict) else {}


# CORS for this path (and the AI API) lives in site_gate; re-exported for the mount.
GatewayCors = site_gate.GatewayCors
