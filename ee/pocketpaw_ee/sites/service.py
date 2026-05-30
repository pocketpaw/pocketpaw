# ee/pocketpaw_ee/sites/service.py — Sites control-plane orchestration. Sole
# owner of Site writes. publish() runs: mint site id + signed key → generate +
# smoke-gate the SvelteKit app (generator_client) → PUT the Worker into the WfP
# dispatch namespace → persist the Site. add_domain()/domain_status() drive
# Cloudflare for SaaS. The generator + Cloudflare client + bundle reader are
# injectable so the orchestration is unit-testable without Bun/workerd/CF.
#
# Tenancy: workspace_id is a required parameter on every function; reads filter
# on it. The signed key is minted per site (reused by the capture endpoint).
#
# CF creds (account id + API token + zone) come from env in v1 (PAW_CF_*); the
# client reads them from settings — it does NOT store per-tenant CF creds in v1
# (see the plan's Phase 2 note + cloudflare_client.py). When per-tenant storage
# lands, the token follows the encrypt-before-Mongo pattern other cloud
# credentials use (_core/crypto.encrypt_json) — never logged, never plaintext.
#
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bson import ObjectId

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
from pocketpaw_ee.sites.domain import HostnameStatus
from pocketpaw_ee.sites.dto import DomainStatusResponse, SiteResponse
from pocketpaw_ee.sites.generator_client import GeneratorClient

# The control plane reads the Worker bundle adapter-cloudflare emits here.
_WORKER_BUNDLE_REL = ".svelte-kit/cloudflare/_worker.js"


def _default_bundle_reader(project_dir: str) -> bytes:
    return Path(project_dir, _WORKER_BUNDLE_REL).read_bytes()


def _capture_base() -> str:
    import os

    return os.environ.get("PAW_CAPTURE_API_BASE", "http://localhost:8888/api/v1")


def _cf_client():
    """Build the real Cloudflare client from settings (env). Injected in tests."""
    import os

    from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

    return CloudflareClient(
        account_id=os.environ["PAW_CF_ACCOUNT_ID"],
        api_token=os.environ["PAW_CF_API_TOKEN"],
        zone_id=os.environ["PAW_CF_ZONE_ID"],
        dispatch_namespace=os.environ.get("PAW_CF_DISPATCH_NAMESPACE", "paw-sites"),
    )


def _to_response(doc: _SiteDoc) -> SiteResponse:
    return SiteResponse(
        id=str(doc.id),
        pocket_id=doc.pocket_id,
        name=doc.name,
        script_name=doc.script_name,
        deployed=doc.deployed,
        signed_key=doc.signed_key,
    )


async def publish(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    ripple_spec: dict[str, Any],
    theme: dict[str, Any],
    name: str = "",
    _generator: GeneratorClient | None = None,
    _cloudflare: Any | None = None,
    _bundle_reader: Callable[[str], bytes] = _default_bundle_reader,
) -> _SiteDoc:
    """Generate, smoke-gate, deploy, and persist a site. Raises SmokeGateFailed
    (from generator_client) if the workerd smoke render fails — the site is not
    deployed and not persisted as deployed."""
    generator = _generator or GeneratorClient()
    cf = _cloudflare or _cf_client()

    site_id = str(ObjectId())
    signed_key = f"site_key_{secrets.token_urlsafe(24)}"

    build = await generator.build(
        ripple_spec=ripple_spec,
        theme=theme,
        site_id=site_id,
        title=name or "Untitled site",
        capture_api_base=_capture_base(),
        capture_signed_key=signed_key,
    )

    bundle = _bundle_reader(build.project_dir)
    await cf.put_worker(script_name=site_id, bundle=bundle)

    doc = _SiteDoc(
        id=ObjectId(site_id),
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner=user_id,
        name=name,
        script_name=site_id,
        deployed=True,
        signed_key=signed_key,
    )
    await doc.insert()
    return doc


async def _load(workspace_id: str, site_id: str) -> _SiteDoc:
    doc = await _SiteDoc.find_one({"_id": ObjectId(site_id), "workspace": workspace_id})
    if doc is None:
        raise NotFound("site", site_id)
    return doc


async def add_domain(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Register a custom hostname with Cloudflare for SaaS and store it on the
    site. Returns the ONE CNAME the client pastes at their registrar."""
    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    ch = await cf.create_custom_hostname(hostname)
    site.domains.append(
        _SiteDomainDoc(
            hostname=ch.hostname,
            cf_hostname_id=ch.id,
            cname_target=ch.cname_target,
            status=ch.status.value,
        )
    )
    await site.save()
    return DomainStatusResponse(
        hostname=ch.hostname, cname_target=ch.cname_target, status=ch.status.value
    )


async def domain_status(
    *,
    workspace_id: str,
    site_id: str,
    hostname: str,
    _cloudflare: Any | None = None,
) -> DomainStatusResponse:
    """Poll Cloudflare for the hostname's current status and persist it."""
    cf = _cloudflare or _cf_client()
    site = await _load(workspace_id, site_id)
    dom = next((d for d in site.domains if d.hostname == hostname), None)
    if dom is None:
        raise NotFound("domain", hostname)
    status: HostnameStatus = await cf.get_hostname_status(dom.cf_hostname_id)
    dom.status = status.value
    await site.save()
    return DomainStatusResponse(
        hostname=dom.hostname, cname_target=dom.cname_target, status=status.value
    )


async def list_for_workspace(workspace_id: str) -> list[SiteResponse]:
    cursor = _SiteDoc.find({"workspace": workspace_id}).sort(-_SiteDoc.createdAt)  # type: ignore[operator]
    return [_to_response(doc) async for doc in cursor]


__all__ = ["publish", "add_domain", "domain_status", "list_for_workspace"]
