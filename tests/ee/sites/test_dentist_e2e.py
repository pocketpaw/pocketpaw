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
# Updated 2026-06-06 (feat/sites-publish-deploy-wire — CF deploy seam): publish()
# now goes through build_and_deploy(); the fakes expose build_and_deploy
# (dispatching to the CF target) + deploy_site instead of build() + put_worker.
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


class _FakeGenerator:
    async def build_and_deploy(self, *, cloudflare=None, local_deploy=None, **kw):
        from pocketpaw_ee.sites.generator_client import DeployResult

        if cloudflare is not None:
            url = await cloudflare.deploy_site(
                script_name=kw["site_id"], project_dir="/tmp/dentist"
            )
        elif local_deploy is not None:
            url = local_deploy(kw["site_id"], "/tmp/dentist")
        else:
            return DeployResult(success=False, error="no deploy target")
        return DeployResult(success=True, url=url)


class _FakeCF:
    async def deploy_site(self, *, script_name, project_dir):
        return f"https://paw-sites.workers.dev/{script_name}/"

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


@pytest.mark.asyncio
async def test_published_site_captures_lead_with_no_manual_mongo_edit(beanie_test_db, capture_app):
    """Phase 2 proof: a site published through the REAL publish() flow — with NO
    hand-edit of allowed_origins or event_mapping — captures a basic
    {full_name, phone} lead. This is the exact shape the generated /api/submit
    endpoint forwards: form_type "lead", JSON body, Origin set to the site's own
    host (localhost here), signed_key == the key publish() minted. The earlier
    dentist test had to set site.allowed_origins + site.event_mapping by hand;
    this one asserts publish()'s seeded defaults make that unnecessary."""
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
    # NO manual edits here — straight to a capture POST.

    # The generated endpoint forwards JSON with Origin = the site's own origin.
    # Locally that origin is http://localhost:5173 (host "localhost"), which is in
    # the default allowed_origins publish() seeded.
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/v1/sites/{site.script_name}/capture",
            json={
                "form_type": "lead",
                "payload": {
                    "full_name": "Dana Lee",
                    "phone": "775-555-0199",
                    "company_website": "",  # empty honeypot
                },
                "submitter_ref": "",
                "signed_key": site.signed_key,  # == the key publish() minted
            },
            headers={"origin": "http://localhost:5173"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["lead_id"]

    # The lead landed, tenant-scoped, with the default mapping applied.
    from pocketpaw_ee.cloud.leads import service as leads_service

    leads = await leads_service.list_for_site("ws_dentist", site.script_name)
    assert len(leads) == 1
    assert leads[0].form_type == "lead"
    # full_name + phone mapped through. email + message were absent from the
    # payload: a FULL-match placeholder ("{{ payload.email }}") that resolves to a
    # missing key returns None (only partial-substitution templates coerce to ""),
    # so those keys are present-but-None. The lead still lands — that is the proof.
    assert leads[0].properties["full_name"] == "Dana Lee"
    assert leads[0].properties["phone"] == "775-555-0199"
    assert leads[0].properties["email"] is None
    assert leads[0].properties["message"] is None
