# ee/pocketpaw_ee/integrations/site_gate.py: the gates every site-facing route shares.
#
# A published site reaches us on two public surfaces: the integrations gateway
# (``/sites/{id}/x/...``, gateway.py) and the AI API (``/sites/{id}/ai/...``,
# ``pocketpaw_ee.site_ai``). Both authenticate the same way, so the primitives live
# here and each surface layers its own checks on top:
#
#   * ``authenticate``: one of
#       - ``X-Paw-Site-Key``: the site's public embed key (browser). Same credential
#         chain as the concierge (``site_keys.lookup_site_by_key``), the key's site
#         must be ``site_id``, then a fail-closed origin gate over the site's own
#         hosts, or the origin of one of the site's OWN drafts (``own_draft``).
#       - ``Authorization: Bearer <PAW_GATEWAY_TOKEN>`` (the site's Worker, server
#         side), compared in constant time inside ``site_secrets``.
#     Returns ``(site, caller, preview)``; ``caller`` is "site_key" or
#     "gateway_token", ``preview`` is True only for an own-draft origin.
#   * ``verify_turnstile`` / ``site_widget``: the site's own Turnstile widget when it
#     has one, else the platform secret. Both fail closed on a broken widget row.
#   * ``bump``: the atomic per-minute ``SiteRateCounter`` increment.
#   * ``rate_key``: the hashed client address used in counter keys (never raw IPs).
#   * ``GatewayCors``: open CORS (no credentials) for the gateway and AI paths only.
#
# Refusals raise ``SiteGateError(code)``; each surface maps the code to its own
# wire shape. Nothing here logs a credential.

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class SiteGateError(Exception):
    """A shared gate refused the call. ``code`` is one of ``unauthorized``,
    ``origin_denied`` or ``turnstile_required``."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def rate_key(ip: str) -> str:
    return hashlib.sha256(ip.encode("utf-8")).hexdigest() if ip else ""


async def site_by_id(site_id: str):
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


def matches(site: Any, site_id: str) -> bool:
    return site_id in {str(site.id), site.script_name or "\x00"}


async def own_draft(site: Any, origin: str | None) -> bool:
    """True when ``origin`` is a draft preview of ``site``'s own pocket. A draft's
    token names exactly one stored draft, so this admits that pocket's preview and
    nothing else. Fails closed when the store cannot be read."""
    if not origin or not site.pocket_id:
        return False
    from pocketpaw_ee.sites import preview_origin

    try:
        pocket_id = await asyncio.to_thread(preview_origin.draft_pocket_for_origin, origin)
    except Exception:  # noqa: BLE001 - an unreadable store denies, never admits
        logger.warning("site_gate: draft origin lookup failed", exc_info=True)
        return False
    return pocket_id is not None and pocket_id == str(site.pocket_id)


async def authenticate(request: Request, site_id: str) -> tuple[Any, str, bool]:
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-paw-site-key", "")
    if auth.lower().startswith("bearer "):
        from pocketpaw_ee.integrations import bindings
        from pocketpaw_ee.sites import site_secrets

        site = await site_by_id(site_id)
        if site is None or site.revoked or not site.pocket_id:
            raise SiteGateError("unauthorized")
        ok = await site_secrets.secret_matches(
            workspace_id=site.workspace,
            pocket_id=site.pocket_id,
            name=bindings.GATEWAY_TOKEN_NAME,
            candidate=auth[7:].strip(),
        )
        if not ok:
            raise SiteGateError("unauthorized")
        return site, "gateway_token", False
    if key:
        from pocketpaw.sites_capture.ingest import origin_allowed
        from pocketpaw_ee.cloud.auth.site_keys import lookup_site_by_key
        from pocketpaw_ee.cloud.leads.intake import derived_origins

        try:
            site = await lookup_site_by_key(key)
        except HTTPException:
            raise SiteGateError("unauthorized") from None
        if not matches(site, site_id):
            raise SiteGateError("unauthorized")
        origin = request.headers.get("origin") or None
        # The site's OWN hosts only (allowlist, url, custom domains): the owner's
        # extra lead-intake hosts widen lead capture, never who may call the site APIs.
        if origin_allowed(derived_origins(site), origin):
            return site, "site_key", False
        if await own_draft(site, origin):
            return site, "site_key", True
        raise SiteGateError("origin_denied")
    raise SiteGateError("unauthorized")


def turnstile_configured() -> bool:
    from pocketpaw.config import get_settings

    return bool(str(get_settings().turnstile_secret or "").strip())


async def verify_turnstile(token: str, ip: str, widget: tuple[str, list[str]] | None = None):
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud._core.turnstile import verify_turnstile as _verify

    if not token:
        raise SiteGateError("turnstile_required")
    kwargs: dict[str, Any] = {}
    if widget is not None:
        kwargs = {"secret": widget[0], "expected_hostnames": widget[1]}
    try:
        await _verify(token, ip or None, code="turnstile_required", **kwargs)
    except CloudError:
        raise SiteGateError("turnstile_required") from None


async def site_widget(site: Any) -> tuple[str, list[str]] | None:
    """The site's own Turnstile widget, if it has one. Fails closed on a broken row."""
    from pocketpaw_ee.integrations.providers import turnstile

    try:
        return await turnstile.gateway_widget(site)
    except Exception:
        logger.warning("site_gate: site Turnstile widget unreadable", exc_info=True)
        raise SiteGateError("turnstile_required") from None


async def bump(scope: str, scope_id: str) -> int:
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


# ------------------------------------------------------------------- CORS

_SITE_API_PATH = re.compile(r"^/api/v1/sites/[^/]+/(x/[^/]+/[^/]+|ai/[^/]+)/?$")


class GatewayCors:
    """ASGI middleware: CORS for the site-facing paths only, for any site origin.

    A published site calls the gateway and the AI API from its own origin, which
    the app-wide CORS list cannot know. Each surface's own origin gate is the
    access control; this layer only lets the browser deliver the request and read
    the answer. No credentials are allowed, so no cookie ever rides along. Installed
    outermost (``app.state.outermost_middleware``) so the app-wide CORS layer never
    refuses the preflight first."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _SITE_API_PATH.match(scope.get("path", "")):
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
                (b"access-control-allow-headers", b"content-type, x-paw-site-key, x-paw-ai-ticket"),
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
