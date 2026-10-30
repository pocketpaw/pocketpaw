# ee/pocketpaw_ee/integrations/providers/turnstile.py: the built-in ``turnstile`` provider.
#
# Kind ``builtin``, no actions, no owner-editable settings. Linking it gives the site
# its own Cloudflare Turnstile widget, created on OUR Cloudflare account
# (``PAW_CF_ACCOUNT_ID`` + ``PAW_CF_API_TOKEN``, the same credentials the Sites
# deploy uses; the token needs Turnstile edit). Without them, or without
# ``CLOUD_ENCRYPTION_KEY``, availability is ``unconfigured`` and setup is a no-op.
#
# ``site_setup`` (the bindings hook, run on link / settings change / enable) creates
# the widget or re-domains it. Widget domains are the site's deployed host plus its
# live custom domains, capped at ``MAX_DOMAINS`` (the free-plan limit per widget);
# Cloudflare is called only when that host set changed. Setup also re-runs:
#   * before a publish builds (``site_key_for_build``), so the build carries the
#     current sitekey as ``siteConfig.turnstileSiteKey``;
#   * after a publish deploys (``after_publish``), which records the sitekey that
#     build carried as ``deployed_sitekey`` and creates the widget once the site's
#     URL is known (a first publish has no URL when it builds, so its widget lands
#     after it and the NEXT publish carries it);
#   * when a custom domain goes live or is removed (``resync_site``, called from
#     ``sites.service.domain_status`` / ``remove_domain``).
#
# Storage: the widget secret is Fernet-encrypted in ``secret_ciphertext``. The
# create path writes secret + ``sitekey`` + ``widget_domains`` in ONE update that
# only matches a row with no secret yet; a setup that loses that race deletes the
# widget it just made and adopts the winner's. ``sitekey`` / ``widget_domains`` are
# hidden settings (never in a view).
#
# Enforcement: the gateway uses the per-site secret only while ``deployed_sitekey``
# equals the current sitekey, i.e. the live page can render the widget. Until then
# the site reads ``setup_required`` and the gateway keeps its platform-wide rule. A
# Cloudflare error raises out of setup (``setup_required`` too); ``site_teardown``
# deletes the widget.

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
            ciphertext = crypto.encrypt(secret)
            coll = SiteIntegration.get_pymongo_collection()
            won = await coll.update_one(
                {"_id": binding.id, "secret_ciphertext": {"$in": ["", None]}},
                {
                    "$set": {
                        "secret_ciphertext": ciphertext,
                        f"settings.{SITEKEY}": sitekey,
                        f"settings.{DOMAINS}": ",".join(hosts),
                    }
                },
            )
            if not won.modified_count:
                # A concurrent setup stored its widget first: drop ours, use theirs.
                await _cf("DELETE", f"/{sitekey}", creds)
                fresh = await coll.find_one({"_id": binding.id}) or {}
                binding.secret_ciphertext = fresh.get("secret_ciphertext", "") or ""
                binding.settings = dict(fresh.get("settings") or {})
                return {
                    k: binding.settings[k] for k in (SITEKEY, DOMAINS) if binding.settings.get(k)
                }
            binding.secret_ciphertext = ciphertext
            binding.settings = {**settings, SITEKEY: sitekey, DOMAINS: ",".join(hosts)}
        return {SITEKEY: sitekey, DOMAINS: ",".join(hosts)}

    def site_status(self, binding: Any, connection: Any | None) -> str | None:  # noqa: ARG002
        """``setup_required`` until the live build carries the current widget."""
        if not _configured():
            return None
        sitekey = str((binding.settings or {}).get(SITEKEY) or "")
        live = getattr(binding, "deployed_sitekey", "") or ""
        if not binding.secret_ciphertext or not sitekey or live != sitekey:
            return "setup_required"
        return None

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
    try:
        binding = await _binding(workspace_id, pocket_id)
        if binding is None:
            return ""
        await _setup(binding)
    except Exception:  # noqa: BLE001 - a publish never fails on this
        logger.warning("turnstile: publish-time setup failed", exc_info=True)
        return ""
    if not binding.secret_ciphertext:
        return ""
    return str((binding.settings or {}).get(SITEKEY) or "")


async def _setup(binding: Any) -> None:
    from pocketpaw_ee.integrations import bindings
    from pocketpaw_ee.integrations.providers import get_provider

    provider = get_provider(PROVIDER_ID)
    if provider is not None:
        await bindings.run_setup(binding, provider, None, "")


async def after_publish(workspace_id: str, pocket_id: str, baked_sitekey: str) -> None:
    """A publish deployed: record the sitekey its build carried, then re-run setup
    (the site's URL may only now be known). Never raises."""
    try:
        binding = await _binding(workspace_id, pocket_id)
        if binding is None:
            return
        await binding.get_pymongo_collection().update_one(
            {"_id": binding.id}, {"$set": {"deployed_sitekey": baked_sitekey or ""}}
        )
        binding.deployed_sitekey = baked_sitekey or ""
        await _setup(binding)
    except Exception:  # noqa: BLE001 - the site is already live
        logger.warning("turnstile: post-publish setup failed", exc_info=True)


async def resync_site(site: Any) -> None:
    """A custom domain went live or was removed: re-domain the widget. Never raises."""
    try:
        if not site.pocket_id:
            return
        binding = await _binding(site.workspace, site.pocket_id)
        if binding is not None:
            await _setup(binding)
    except Exception:  # noqa: BLE001
        logger.warning("turnstile: domain re-sync failed", exc_info=True)


async def gateway_widget(site: Any) -> tuple[str, list[str]] | None:
    """(widget secret, the site's hostnames) while the site's live build carries its
    own widget. Raises when the stored secret can't be decrypted (callers fail closed)."""
    from pocketpaw_ee.cloud._core import crypto

    if not site.pocket_id:
        return None
    binding = await _binding(site.workspace, site.pocket_id)
    if binding is None or not binding.secret_ciphertext:
        return None
    sitekey = str((binding.settings or {}).get(SITEKEY) or "")
    if not sitekey or binding.deployed_sitekey != sitekey:
        return None
    secret = crypto.decrypt(binding.secret_ciphertext)
    return (secret, site_hostnames(site)) if secret else None
