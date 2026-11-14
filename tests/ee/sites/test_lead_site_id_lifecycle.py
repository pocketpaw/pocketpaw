# tests/ee/sites/test_lead_site_id_lifecycle.py — the site lifecycle (delete, export,
# transfer) finds a site's leads under the SAME id the lead writer filed them under.
#
# Writers file a lead under ``leads.domain.lead_site_id(site)``: the script_name of a
# built site, the object id of a connected (foreign) concierge. Issue #2219 was the
# write half (every foreign concierge filed under ""). These pin both halves: leads go
# in through the real ``capture_internal`` and come out through the real cascade, export
# and transfer, so neither side's id choice can be hand-picked by the test.
#
# A pre-existing row filed under "" (written before the foreign branch existed) must
# survive every lifecycle op: "" names no one site, so nothing may purge or move it.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.leads.domain import lead_site_id
from pocketpaw_ee.cloud.models.lead import Lead, LeadSource
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import delete_cascade, transfer
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.delete_job import _DeleteDeps

WS = "ws_leads"


async def _site(pocket_id: str, *, foreign: bool = False, script_name: str = "") -> Site:
    doc = Site(
        workspace=WS,
        identity_workspace=WS,
        pocket_id=pocket_id,
        owner="u1",
        name=pocket_id,
        script_name=script_name,
        foreign_origin=foreign,
    )
    await doc.insert()
    return doc


async def _lead(site: Site, who: str) -> None:
    lead = await leads_service.capture_internal(
        site=site, form_type="concierge", kind="concierge", properties={"email": who}
    )
    assert lead is not None


async def _legacy_blank_lead() -> None:
    await Lead(
        workspace=WS,
        site_id="",
        form_type="concierge",
        properties={"email": "old@x.com"},
        source=LeadSource(form_type="concierge", site_id=""),
    ).insert()


def _emails(rows: list[Any]) -> list[str]:
    return sorted(r.properties["email"] for r in rows)


async def _delete_records(site: Site) -> None:
    await delete_cascade._purge_records(site=site, deps=_DeleteDeps(cloudflare=None, assets=None))


@pytest.mark.asyncio
async def test_two_foreign_concierges_keep_their_leads_apart(beanie_test_db):
    a = await _site("pk-a", foreign=True)
    b = await _site("pk-b", foreign=True)
    await _lead(a, "a@x.com")
    await _lead(b, "b@x.com")

    assert lead_site_id(a) and lead_site_id(b) and lead_site_id(a) != lead_site_id(b)
    assert _emails(await leads_service.list_for_site(WS, lead_site_id(a))) == ["a@x.com"]
    assert await leads_service.count_for_site(WS, lead_site_id(b)) == 1

    await _delete_records(a)

    assert _emails(await Lead.find_all().to_list()) == ["b@x.com"]


@pytest.mark.asyncio
async def test_deleting_a_built_site_removes_its_leads_and_only_its(beanie_test_db):
    built = await _site("pk-built", script_name="paw-site-abc")
    foreign = await _site("pk-foreign", foreign=True)
    await _lead(built, "built@x.com")
    await _lead(foreign, "foreign@x.com")

    await _delete_records(built)

    assert _emails(await Lead.find_all().to_list()) == ["foreign@x.com"]


@pytest.mark.asyncio
async def test_a_legacy_blank_site_id_lead_survives_every_delete(beanie_test_db):
    await _legacy_blank_lead()
    draft = await _site("pk-draft")  # no script, not foreign: lead_site_id is ""
    foreign = await _site("pk-foreign", foreign=True)

    await _delete_records(draft)
    await _delete_records(foreign)

    assert _emails(await Lead.find_all().to_list()) == ["old@x.com"]


@pytest.mark.asyncio
async def test_exporting_a_built_site_carries_its_leads(beanie_test_db, monkeypatch):
    built = await _site("pk-built", script_name="paw-site-abc")
    await _lead(built, "built@x.com")
    await _legacy_blank_lead()

    async def _static(**_kw: Any) -> Any:
        raise ValidationError("sites.not_dynamic", "static")

    class _Adapter:
        async def put(self, key, stream, mime):
            async for _ in stream:
                pass

    monkeypatch.setattr(sites_service, "_dynamic_pocket_objects", _static)
    monkeypatch.setattr(sites_service, "_export_adapter", lambda: _Adapter())

    await sites_service.create_site_export(workspace_id=WS, user_id="u1", site_id=str(built.id))

    from pocketpaw_ee.cloud.models.site_export import SiteExport

    (export,) = await SiteExport.find_all().to_list()
    assert export.status == "ready"
    assert export.lead_count == 1


@pytest.mark.asyncio
async def test_transferring_a_built_site_moves_its_leads(beanie_test_db):
    built = await _site("pk-built", script_name="paw-site-abc")
    await _lead(built, "built@x.com")
    await _legacy_blank_lead()

    await transfer._move_records(
        site=built, destination="ws_new", deps=sites_service._TransferDeps()
    )

    moved = {r.properties["email"]: r.workspace for r in await Lead.find_all().to_list()}
    assert moved == {"built@x.com": "ws_new", "old@x.com": WS}


@pytest.mark.asyncio
async def test_collecting_leads_for_a_blank_id_collects_nothing(beanie_test_db):
    from pocketpaw_ee.sites.export import collect_leads

    await _legacy_blank_lead()

    assert await collect_leads(workspace_id=WS, site_id="", lead_model=Lead) == []
