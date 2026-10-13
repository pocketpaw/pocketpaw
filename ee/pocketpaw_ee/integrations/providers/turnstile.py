# ee/pocketpaw_ee/integrations/providers/turnstile.py: the built-in ``turnstile`` provider.
#
# Kind ``builtin``, no actions, no owner-editable settings. Linking it gives the site
# its own Cloudflare Turnstile widget, created on OUR Cloudflare account
# (``PAW_CF_ACCOUNT_ID`` + ``PAW_CF_API_TOKEN``, the same credentials the Sites
# deploy uses; the token needs Turnstile edit). Without them, or without
# ``CLOUD_ENCRYPTION_KEY``, availability is ``unconfigured`` and setup is a no-op.
#
# ``site_setup`` (the bindings hook, run on link / settings change / enable) creates
# the widget or re-domains it, and returns ``sitekey`` + ``widget_domains`` as
# hidden settings (merged by ``settings.server_values``, never in a view). A publish
# re-runs setup through ``site_key_for_build`` so a site linked before its first
# deploy, or one that gained a custom domain, is covered; Cloudflare is called only
# when the host set changed. Widget domains are the site's deployed host plus its
# live custom domains, capped at ``MAX_DOMAINS`` (the free-plan limit per widget).
#
# The widget secret is Fernet-encrypted in ``SiteIntegration.secret_ciphertext``
# (written by setup with its own atomic $set) and only decrypted by
# ``gateway_widget`` for siteverify. A Cloudflare error raises out of setup, so the
# site reads ``setup_required``; until a widget exists the gateway keeps its
# platform-wide Turnstile behaviour. ``site_teardown`` deletes the widget.

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from pocketpaw_ee.integrations.providers.base import (
    Availability,
    BaseProvider,
    SettingsField,
    SetupContext,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "turnstile"
MAX_DOMAINS = 10
SITEKEY = "sitekey"
DOMAINS = "widget_domains"

_CF_API = "https://api.cloudflare.com/client/v4"
# Tests swap in an ``httpx.MockTransport``.
_transport: httpx.AsyncBaseTransport | None = None


def _cf_creds() -> tuple[str, str] | None:
    account = os.environ.get("PAW_CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("PAW_CF_API_TOKEN", "").strip()
    return (account, token) if account and token else None


def _configured() -> bool:
    from pocketpaw_ee.cloud._core import crypto

    return _cf_creds() is not None and crypto.is_configured()


# ------------------------------------------------------------------ hosts


def _host(value: str) -> str:
    host = (value or "").strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.split("/", 1)[0].split(":", 1)[0].rstrip(".")


def site_hostnames(site: Any) -> list[str]:
    """The hosts a site is served from: its deployed url and live custom domains."""
    hosts = [_host(site.url or "")]
    hosts.extend(
        _host(d.hostname)
        for d in (site.domains or [])
        if d.hostname and getattr(d, "status", "") == "live"
    )
    return [h for h in dict.fromkeys(hosts) if h and h not in {"localhost", "127.0.0.1"}]


async def _site_for(workspace_id: str, pocket_id: str):
    from pocketpaw_ee.cloud.models.site import Site

    return await Site.find_one(
        Site.workspace == workspace_id,
        Site.pocket_id == pocket_id,
        Site.revoked != True,  # noqa: E712
    )


# ------------------------------------------------------------- Cloudflare


async def _cf(method: str, path: str, creds: tuple[str, str], body: dict | None = None) -> dict:
    account, token = creds
    url = f"{_CF_API}/accounts/{account}/challenges/widgets{path}"
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, transport=_transport, timeout=15.0
    ) as client:
        resp = await client.request(method, url, json=body)
    if method == "DELETE" and resp.status_code == 404:
        return {}
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code // 100 != 2 or not data.get("success", False):
        codes = [e.get("code") for e in data.get("errors", []) if isinstance(e, dict)]
        raise RuntimeError(f"Cloudflare Turnstile API {resp.status_code} {codes}")
    return data.get("result") or {}


def _widget_body(site: Any, domains: list[str]) -> dict:
    return {"name": f"paw-site-{site.id}"[:254], "domains": domains, "mode": "managed"}


def _split(value: Any) -> list[str]:
    return [h for h in str(value or "").split(",") if h]


# --------------------------------------------------------------- provider


class TurnstileProvider(BaseProvider):
    id = PROVIDER_ID
    name = "Bot protection"
    kind = "builtin"
    category = "security"
    plan = "free"
    actions = {}
    settings_fields = (
        SettingsField(key=SITEKEY, label="Site key", hidden=True, max_length=200),
        SettingsField(key=DOMAINS, label="Widget domains", hidden=True, max_length=2600),
    )

    def availability(self) -> Availability:
        return "available" if _configured() else "unconfigured"

    async def site_setup(self, binding: Any, ctx: SetupContext) -> dict[str, Any]:  # noqa: ARG002
        """Create or re-domain the site's widget. Idempotent; raises on a CF error."""
        from pocketpaw_ee.cloud._core import crypto
        from pocketpaw_ee.cloud.models.integration import SiteIntegration

        settings = dict(binding.settings or {})
        creds = _cf_creds()
        if creds is None or not crypto.is_configured():
            return {}
        site = await _site_for(binding.workspace, binding.pocket_id)
        hosts = site_hostnames(site) if site is not None else []
        if not hosts:
            return {}  # nothing deployed yet; the next publish runs setup again
        if len(hosts) > MAX_DOMAINS:
            logger.warning(
                "turnstile: site %s has %d hostnames, widget keeps the first %d",
                site.id,
                len(hosts),
                MAX_DOMAINS,
            )
            hosts = hosts[:MAX_DOMAINS]
        sitekey = str(settings.get(SITEKEY) or "")
        if sitekey and binding.secret_ciphertext:
            if sorted(_split(settings.get(DOMAINS))) != sorted(hosts):
                await _cf("PUT", f"/{sitekey}", creds, _widget_body(site, hosts))
        else:
            result = await _cf("POST", "", creds, _widget_body(site, hosts))
            sitekey = str(result.get("sitekey") or "")
            secret = str(result.get("secret") or "")
            if not sitekey or not secret:
                raise RuntimeError("Cloudflare Turnstile API returned no sitekey/secret")
            binding.secret_ciphertext = crypto.encrypt(secret)
            await SiteIntegration.get_pymongo_collection().update_one(
                {"_id": binding.id}, {"$set": {"secret_ciphertext": binding.secret_ciphertext}}
            )
        return {SITEKEY: sitekey, DOMAINS: ",".join(hosts)}

    async def site_teardown(self, binding: Any) -> None:
        """Delete the site's widget on Cloudflare."""
        sitekey = str((binding.settings or {}).get(SITEKEY) or "")
        creds = _cf_creds()
        if sitekey and creds is not None:
            await _cf("DELETE", f"/{sitekey}", creds)


# ------------------------------------------------------------------ reads


async def _binding(workspace_id: str, pocket_id: str):
    from pocketpaw_ee.cloud.models.integration import SiteIntegration as Doc

    return await Doc.find_one(
        Doc.workspace == workspace_id, Doc.pocket_id == pocket_id, Doc.provider == PROVIDER_ID
    )


async def site_key_for_build(workspace_id: str, pocket_id: str) -> str:
    """``siteConfig.turnstileSiteKey`` for a publish: "" unless the site is linked.

    Re-runs setup first, so the widget covers the hosts the site has right now."""
    from pocketpaw_ee.integrations import bindings
    from pocketpaw_ee.integrations.providers import get_provider

    try:
        binding = await _binding(workspace_id, pocket_id)
        provider = get_provider(PROVIDER_ID)
        if binding is None or provider is None:
            return ""
        await bindings.run_setup(binding, provider, None, "")
    except Exception:  # noqa: BLE001 - a publish never fails on this
        logger.warning("turnstile: publish-time setup failed", exc_info=True)
        return ""
    if not binding.secret_ciphertext:
        return ""
    return str((binding.settings or {}).get(SITEKEY) or "")


async def gateway_widget(site: Any) -> tuple[str, list[str]] | None:
    """(widget secret, the site's hostnames) when ``site`` has its own widget."""
    from pocketpaw_ee.cloud._core import crypto

    if not site.pocket_id:
        return None
    binding = await _binding(site.workspace, site.pocket_id)
    if binding is None or not binding.secret_ciphertext:
        return None
    secret = crypto.decrypt(binding.secret_ciphertext)
    return (secret, site_hostnames(site)) if secret else None
