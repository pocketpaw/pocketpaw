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
#          fail-closed origin gate over the site's allowlist + own hosts
#                                                          -> unauthorized 401 / origin_denied 403
#        Authorization: Bearer <PAW_GATEWAY_TOKEN> (project Worker, server side),
#          compared in constant time inside ``site_secrets``  -> unauthorized 401
#   3. provider registered, action declared + public, site bound, action enabled,
#      connect kinds have an active grant      -> not_connected 409 / action_disabled 403
#   4. Turnstile on write actions, site-key callers only, when a Turnstile secret
#      is configured                                       -> turnstile_required 403
#   5. ``SiteRateCounter`` per (site, action) and per (site, action, IP hash)
#                                                          -> rate_limited 429
#   6. typed args (extra keys refused, string caps)        -> invalid_args 422
#   7. handler, with a token from ``vault.get_valid_token`` -> provider_error 502
#
# The site-key path does NOT use ``resolve_site_key_with_site``: that resolver also
# refuses sites without a live concierge, which would switch integrations off for
# most sites. It reuses the same key lookup and ``origin_allowed`` primitives.
# A response never carries a token; handlers return safe results only.

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw_ee.integrations import bindings, vault
from pocketpaw_ee.integrations.providers import get_provider
from pocketpaw_ee.integrations.providers.base import ActionContext, ProviderError

logger = logging.getLogger(__name__)

STATUS = {
    "unauthorized": 401,
    "origin_denied": 403,
    "not_connected": 409,
    "action_disabled": 403,
    "turnstile_required": 403,
    "rate_limited": 429,
    "invalid_args": 422,
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


def _rate_key(ip: str) -> str:
    return hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else ""


async def _site_by_id(site_id: str):
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.site import Site

    if not isinstance(site_id, str) or not site_id or len(site_id) > 128:
        return None
    try:
        site = await Site.get(PydanticObjectId(site_id))
    except Exception:  # noqa: BLE001 - not an ObjectId: try the worker script name
        site = None
    if site is None:
        site = await Site.find_one({"script_name": site_id})
    return site


def _matches(site: Any, site_id: str) -> bool:
    return site_id in {str(site.id), site.script_name or "\x00"}


async def _authenticate(request: Request, site_id: str) -> tuple[Any, str]:
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-paw-site-key", "")
    if auth.lower().startswith("bearer "):
        from pocketpaw_ee.sites import site_secrets

        site = await _site_by_id(site_id)
        if site is None or site.revoked or not site.pocket_id:
            raise GatewayError("unauthorized")
        ok = await site_secrets.secret_matches(
            workspace_id=site.workspace,
            pocket_id=site.pocket_id,
            name=bindings.GATEWAY_TOKEN_NAME,
            candidate=auth[7:].strip(),
        )
        if not ok:
            raise GatewayError("unauthorized")
        return site, "gateway_token"
    if key:
        from pocketpaw.sites_capture.ingest import origin_allowed
        from pocketpaw_ee.cloud.auth.site_keys import lookup_site_by_key
        from pocketpaw_ee.cloud.leads.router import _effective_origins

        try:
            site = await lookup_site_by_key(key)
        except HTTPException:
            raise GatewayError("unauthorized") from None
        if not _matches(site, site_id):
            raise GatewayError("unauthorized")
        if not origin_allowed(_effective_origins(site), request.headers.get("origin") or None):
            raise GatewayError("origin_denied")
        return site, "site_key"
    raise GatewayError("unauthorized")


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


def _turnstile_configured() -> bool:
    from pocketpaw.config import get_settings

    return bool(str(get_settings().turnstile_secret or "").strip())


async def _verify_turnstile(token: str, ip: str) -> None:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud._core.turnstile import verify_turnstile

    if not token:
        raise GatewayError("turnstile_required")
    try:
        await verify_turnstile(token, ip or None, code="turnstile_required")
    except CloudError:
        raise GatewayError("turnstile_required") from None


async def _bump(scope: str, scope_id: str) -> int:
    """Atomic per-minute counter, the ``leads.service._bump`` pattern."""
    from pocketpaw_ee.cloud.models.site_rate_counter import SiteRateCounter

    now = datetime.now(UTC)
    coll = SiteRateCounter.get_pymongo_collection()
    doc = await coll.find_one_and_update(
        {"scope": scope, "scope_id": scope_id, "bucket": now.replace(second=0, microsecond=0)},
        {"$inc": {"hits": 1}, "$setOnInsert": {"created_at": now}},
        upsert=True,
        return_document=True,
    )
    return int(doc["hits"])


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
    if spec.write and caller == "site_key" and _turnstile_configured():
        await _verify_turnstile(turnstile_token, ip)

    await _rate_limit(site, provider.id, action, spec, caller, ip)

    _check_strings(args)
    try:
        parsed = spec.args.model_validate(args)
    except PydanticValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "args"
        raise GatewayError("invalid_args", f"`{where}` is not valid.") from None

    async def _token() -> str:
        return await vault.get_valid_token(conn)

    ctx = ActionContext(
        workspace_id=site.workspace,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        provider=provider.id,
        action=action,
        settings=dict(binding.settings or {}),
        caller=caller,  # type: ignore[arg-type]
        get_token=_token if conn is not None else None,
    )
    try:
        result = await spec.handler(ctx, parsed)
    except vault.NotConnected:
        raise GatewayError("not_connected") from None
    except ProviderError as exc:
        logger.info("integrations: %s/%s provider error: %s", provider.id, action, exc)
        raise GatewayError("provider_error") from None
    except Exception:
        logger.exception("integrations: %s/%s handler crashed", provider.id, action)
        raise GatewayError("provider_error") from None
    return result if isinstance(result, dict) else {}


# ------------------------------------------------------------------- CORS

_GATEWAY_PATH = re.compile(r"^/api/v1/sites/[^/]+/x/[^/]+/[^/]+/?$")


class GatewayCors:
    """ASGI middleware: CORS for the gateway path only, for any site origin.

    A published site calls the gateway from its own origin, which the app-wide CORS
    list cannot know. The gateway's own origin gate (step 2) is the access control;
    this layer only lets the browser deliver the request and read the answer. No
    credentials are allowed, so no cookie ever rides along. Installed outermost
    (``app.state.outermost_middleware``) so the app-wide CORS layer never refuses
    the preflight first."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _GATEWAY_PATH.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        origin = ""
        for k, v in scope.get("headers") or []:
            if k == b"origin":
                origin = v.decode("latin-1")
                break
        if not origin:
            await self.app(scope, receive, send)
            return
        cors = [
            (b"access-control-allow-origin", origin.encode("latin-1")),
            (b"vary", b"Origin"),
        ]
        if scope.get("method") == "OPTIONS":
            headers = [
                *cors,
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (b"access-control-allow-headers", b"content-type, x-paw-site-key"),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"0"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                drop = {b"access-control-allow-origin", b"access-control-allow-credentials"}
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in drop]
                message = {**message, "headers": [*kept, *cors]}
            await send(message)

        await self.app(scope, receive, _send)
