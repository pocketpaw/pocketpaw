# tests/cloud/leads/test_service.py — uses the shared mongo_db fixture
# (mongomock-motor) per the cloud testing convention.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.3): covers the
# tenant-scoped Lead capture service — happy-path interpolation write, honeypot
# drop, and cross-tenant read isolation. Exercises Lead/Site through the Beanie
# init fixture (the docs can't be bare-constructed before init_beanie).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.models.site import Site


async def _site(ws="ws1", site_id="site_1", **over) -> Site:
    site = Site(
        workspace=ws,
        pocket_id="pk1",
        owner="u1",
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="pp_tok_x",
        event_mapping={
            "AppointmentRequest": {
                "creates": "AppointmentRequest",
                "fields": {"name": "{{ payload.full_name }}"},
            }
        },
        **over,
    )
    await site.insert()
    return site


@pytest.mark.asyncio
async def test_capture_writes_a_tenant_scoped_lead(mongo_db):
    site = await _site()
    lead = await leads_service.capture(
        site=site,
        form_type="AppointmentRequest",
        payload={"full_name": "Sam", "company_website": ""},
        submitter_ref="ip_hash_1",
    )
    assert lead is not None
    assert lead.workspace_id == "ws1"
    assert lead.site_id == "site_1"
    # event-mapping interpolation produced the resolved property
    assert lead.properties == {"name": "Sam"}


@pytest.mark.asyncio
async def test_capture_drops_honeypot_submission(mongo_db):
    site = await _site()
    lead = await leads_service.capture(
        site=site,
        form_type="AppointmentRequest",
        payload={"full_name": "Bot", "company_website": "spam"},
        submitter_ref="ip_hash_2",
    )
    assert lead is None  # honeypot tripped → silently dropped
    assert await leads_service.count_for_site("ws1", "site_1") == 0


@pytest.mark.asyncio
async def test_list_for_site_is_tenant_scoped(mongo_db):
    site_a = await _site(ws="ws1", site_id="site_a")
    site_b = await _site(ws="ws2", site_id="site_b")
    await leads_service.capture(
        site=site_a, form_type="AppointmentRequest", payload={"full_name": "A"}, submitter_ref="i1"
    )
    await leads_service.capture(
        site=site_b, form_type="AppointmentRequest", payload={"full_name": "B"}, submitter_ref="i2"
    )
    leads_ws1 = await leads_service.list_for_site("ws1", "site_a")
    assert len(leads_ws1) == 1
    assert leads_ws1[0].properties == {"name": "A"}
    # cross-tenant read returns nothing
    assert await leads_service.list_for_site("ws1", "site_b") == []
