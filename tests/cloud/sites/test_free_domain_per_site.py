# tests/cloud/sites/test_free_domain_per_site.py — every free site gets its own
# custom domain; one site holding a domain never uses up a sibling's.
#
# Pricing is per site, so the free allowance is per site too: each free site may
# carry one domain (apex + ``www``). This file pins that from both sides, the
# attach gate (``add_domain``) and the entitlements read the Domains page uses to
# grey its button, because the two must never disagree.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

pytestmark = pytest.mark.usefixtures("mongo_db")


class _CF:
    async def create_custom_hostname(self, hostname: str, *, features=None) -> CustomHostname:
        return CustomHostname(
            id=f"ch_{hostname}",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def create_worker_route(self, *, pattern: str, script: str) -> str:
        return "route_1"

    async def delete_custom_hostname(self, hostname_id: str) -> None:
        return None


def _enforce(monkeypatch) -> None:
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=True, dodo_site_products=None),
    )


async def _free_site(ws: str, pocket_id: str) -> str:
    doc = Site(
        workspace=ws,
        pocket_id=pocket_id,
        owner="u1",
        name=f"Site {pocket_id}",
        plan_tier=site_plans.BASE_SITE_PLAN_KEY,
        subscription_status="none",
        deployed=True,
    )
    await doc.insert()
    return str(doc.id)


async def _attach(ws: str, site_id: str, hostname: str) -> None:
    await sites_service.add_domain(
        workspace_id=ws, site_id=site_id, hostname=hostname, _cloudflare=_CF()
    )


async def test_a_second_free_site_gets_its_own_domain(monkeypatch):
    _enforce(monkeypatch)
    ws = "ws_per_site"
    first = await _free_site(ws, "pk_1")
    second = await _free_site(ws, "pk_2")
    await _attach(ws, first, "first.com")

    await _attach(ws, second, "second.com")
    await _attach(ws, second, "www.second.com")

    doc = await Site.get(second)
    assert [d.hostname for d in doc.domains] == ["second.com", "www.second.com"]


async def test_entitlements_offer_the_slot_while_a_sibling_holds_a_domain(monkeypatch):
    _enforce(monkeypatch)
    ws = "ws_per_site_ent"
    first = await _free_site(ws, "pk_1")
    second = await _free_site(ws, "pk_2")
    await _attach(ws, first, "first.com")

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=second)

    assert ent.custom_domain is True
    assert ent.domain_slots_available is True


async def test_entitlements_close_the_slot_once_this_site_is_full(monkeypatch):
    """The per-site ceiling still holds: apex + www, then the button greys."""
    _enforce(monkeypatch)
    ws = "ws_per_site_full"
    site = await _free_site(ws, "pk_1")
    for host in ["acme.com", "www.acme.com"][: site_plans.free_max_hostnames_per_site()]:
        await _attach(ws, site, host)

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=site)

    assert ent.custom_domain is True
    assert ent.domain_slots_available is False
