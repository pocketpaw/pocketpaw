# tests/ee/sites/test_dentist_e2e.py — the RFC 12 thin-slice acceptance test
# (Task 5.2). Generator + Cloudflare are FAKED; the capture path (origin pin,
# signed key, honeypot, event mapping, tenant write) and the Leads read are
# REAL. One test builds a marketing pocket → publishes (no Bun/workerd/CF) →
# adds a custom domain → submits a form through the real public capture
# endpoint → asserts the lead lands tenant-scoped and cross-tenant isolation
# holds.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 5.2).
#
# Two deliberate deltas from the plan's literal Task 5.2 snippet, both required
# for the test to actually run/pass against the shipped code (the gates, not the
# data, are correct — so the DATA was adjusted, per the task brief):
#
#   1. Fixture: the snippet requests ``mongo_db``, but that fixture is defined
#      only in ``tests/cloud/conftest.py`` and does not reach ``tests/ee/``.
#      This file lives in ``tests/ee/sites/`` (the plan's path), whose conftest
#      (``tests/ee/conftest.py``) exposes the functionally-identical
#      ``beanie_test_db`` — same mongomock-motor init over ``ALL_DOCUMENTS``
#      (which registers both Site and Lead). The sibling ``test_service.py``
#      already uses ``beanie_test_db``; we follow that convention.
#
#   2. Origin allowlist: the snippet seeds ``allowed_origins =
#      ["brightsmiledental.com"]`` (apex) but posts the form from
#      ``Origin: https://www.brightsmiledental.com`` (the ``www`` host).
#      ``sites_capture.ingest.origin_allowed`` does an EXACT host-only match
#      (``host in allowed_origins``), so apex ≠ ``www`` would 403 the happy
#      path. The site is published AND the custom domain is added as
#      ``www.brightsmiledental.com``, so the allowlist is set to that same host
#      — the origin the deployed form genuinely posts from. Clean payload
#      ("Sam Rivera" / "775-555-0100") scans NONE on the InjectionScanner, the
#      empty honeypot field is untripped, and the body is well under the 8 KB
#      payload cap, so every security gate is satisfied by valid input.
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/dentist", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle):
        return True

    async def create_custom_hostname(self, hostname):
        return CustomHostname(
            id="ch_1",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def get_hostname_status(self, hostname_id):
        return HostnameStatus.LIVE


@pytest.fixture
def capture_app():
    from fastapi import FastAPI
    from pocketpaw_ee.cloud.leads.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_dentist_thin_slice_end_to_end(beanie_test_db, capture_app):
    # 1. Publish the dentist pocket (generator + CF faked) → a deployed Site.
    #    rippleSpec is constructed INLINE here — this test does not read the 5.1
    #    JSON fixture.
    site = await sites_service.publish(
        workspace_id="ws_dentist",
        user_id="freelancer_1",
        pocket_id="pk_dentist",
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile Dental",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
    )
    assert site.deployed

    # 1b. Configure capture: origin allowlist, signed key already minted by
    #     publish(), event mapping. The allowlist holds the exact host the
    #     deployed form posts from (www.brightsmiledental.com).
    site.allowed_origins = ["www.brightsmiledental.com"]
    site.event_mapping = {
        "AppointmentRequest": {
            "creates": "AppointmentRequest",
            "fields": {"name": "{{ payload.full_name }}", "phone": "{{ payload.phone }}"},
        }
    }
    await site.save()

    # 2. Add the custom domain → one CNAME to paste; poll → Live.
    dom = await sites_service.add_domain(
        workspace_id="ws_dentist",
        site_id=str(site.id),
        hostname="www.brightsmiledental.com",
        _cloudflare=_FakeCF(),
    )
    assert dom.cname_target == "zone_1.cdn.cloudflare.net"
    status = await sites_service.domain_status(
        workspace_id="ws_dentist",
        site_id=str(site.id),
        hostname="www.brightsmiledental.com",
        _cloudflare=_FakeCF(),
    )
    assert status.status == "live"

    # 3. A patient submits the form (the edge Queue would POST exactly this).
    #    Valid Origin (in the allowlist), correct signed_key, empty honeypot,
    #    clean PII-shaped payload, body well under the 8 KB cap.
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/v1/sites/{site.script_name}/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {
                    "full_name": "Sam Rivera",
                    "phone": "775-555-0100",
                    "company_website": "",
                },
                "submitter_ref": "ip_hash_patient",
                "signed_key": site.signed_key,
            },
            headers={"origin": "https://www.brightsmiledental.com"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 4. The lead lands in the tenant store, scoped to the dentist's workspace.
    from pocketpaw_ee.cloud.leads import service as leads_service

    leads = await leads_service.list_for_site("ws_dentist", site.script_name)
    assert len(leads) == 1
    assert leads[0].properties == {"name": "Sam Rivera", "phone": "775-555-0100"}

    # 5. Cross-tenant isolation: a different workspace sees nothing.
    assert await leads_service.list_for_site("ws_other", site.script_name) == []
