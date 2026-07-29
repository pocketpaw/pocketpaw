# tests/cloud/leads/test_router.py — exercises capture hardening at the router
# (origin pinning + signed key live here) plus the authed read. Pattern: build
# an app that mounts the leads router; for the public endpoint, no auth needed;
# for the read endpoint, follow the existing cloud router test pattern for
# injecting an authed user + active workspace.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.4): router-level
# tests for the public capture surface — wrong-origin reject, bad-signed-key
# reject, and the happy-path accept.
# Updated 2026-05-30 (security hardening): added C1 oversized-payload→413 (no
# lead written) and H1 constant-time signed-key compare (secrets.compare_digest
# is the mechanism; valid key still 200, bad key still 401) coverage.
# Updated 2026-07-22 (SI-4 — feat/sites-import-endpoint): added coverage for the
# NATIVE-FORM capture sibling POST /capture/form (final URL /api/v1/capture/form)
# — the endpoint imported sites' rewired <form>s post to as urlencoded, with
# hidden paw_site_id / paw_key / paw_page / paw_redirect fields. Valid key →
# recorded through the SAME capture pipeline + 303 back to Origin+paw_redirect;
# bad key → 401; absolute / protocol-relative paw_redirect → 400 (open-redirect
# guard); wrong origin → 403; urlencoded content-type accepted natively.
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.site import Site


@pytest.fixture
def capture_app():
    from fastapi import FastAPI
    from pocketpaw_ee.cloud.leads.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


async def _site(ws="ws1", site_id="site_1") -> Site:
    site = Site(
        workspace=ws,
        pocket_id="pk1",
        owner="u1",
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="key_ok",
        event_mapping={
            "AppointmentRequest": {
                "creates": "AppointmentRequest",
                "fields": {"name": "{{ payload.full_name }}"},
            }
        },
    )
    await site.insert()
    return site


@pytest.mark.asyncio
async def test_capture_rejects_wrong_origin(mongo_db, capture_app):
    await _site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://evil.example.com"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_capture_rejects_bad_signed_key(mongo_db, capture_app):
    await _site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "WRONG",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_capture_accepts_valid_submission(mongo_db, capture_app):
    await _site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["lead_id"]


@pytest.mark.asyncio
async def test_capture_rejects_oversized_payload(mongo_db, capture_app):
    """C1: an oversized body (> MAX_PAYLOAD_BYTES) is rejected with 413 and no
    lead is written — the size cap is enforced before the service is called."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    from pocketpaw.sites_capture.models import MAX_PAYLOAD_BYTES

    site = await _site()
    # A single field whose value alone blows past the 8KB cap.
    big_value = "x" * (MAX_PAYLOAD_BYTES + 1024)
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": big_value},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 413
    # The oversized submission never reached the persist path.
    assert await leads_service.count_for_site(site.workspace, "site_1") == 0


@pytest.mark.asyncio
async def test_capture_uses_constant_time_key_compare(mongo_db, capture_app, monkeypatch):
    """H1: the signed-key check goes through secrets.compare_digest (a
    constant-time comparison), not a plain ``!=``. We spy on compare_digest and
    assert the valid-key path both invokes it and still returns 200."""
    import secrets as _secrets

    import pocketpaw_ee.cloud.leads.router as router_mod

    await _site()
    calls: list[tuple[str, str]] = []
    real = _secrets.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(router_mod.secrets, "compare_digest", _spy)

    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/site_1/capture",
            json={
                "form_type": "AppointmentRequest",
                "payload": {"full_name": "Sam"},
                "submitter_ref": "ip1",
                "signed_key": "key_ok",
            },
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 200
    assert calls, "signed-key check must route through secrets.compare_digest"
    assert ("key_ok", "key_ok") in calls


# --------------------------------------------------------------------------- #
# SI-4 — native-form capture: POST /capture/form (final URL /api/v1/capture/form).
# Imported sites rewire their <form>s to a plain urlencoded POST here, with hidden
# paw_site_id / paw_key / paw_page / paw_redirect fields. Same hardening ladder as
# the JSON path, then a 303 back to Origin + the RELATIVE paw_redirect.
# --------------------------------------------------------------------------- #


async def _form_site(ws="ws1", site_id="site_form") -> Site:
    """A site seeded the way create/publish seeds it: a 'lead' event mapping (the
    default form_type the rewired form posts under)."""
    site = Site(
        workspace=ws,
        pocket_id="pk_form",
        owner="u1",
        script_name=site_id,
        allowed_origins=["brightsmiledental.com"],
        signed_key="key_ok",
        event_mapping={
            "lead": {
                "creates": "Lead",
                "fields": {"full_name": "{{ payload.full_name }}", "email": "{{ payload.email }}"},
            }
        },
    )
    await site.insert()
    return site


def _form_fields(**overrides) -> dict:
    fields = {
        "full_name": "Sam Smiles",
        "email": "sam@example.com",
        "paw_site_id": "site_form",
        "paw_key": "key_ok",
        "paw_page": "index.html",
        "paw_redirect": "/thanks.html",
    }
    fields.update(overrides)
    return fields


@pytest.mark.asyncio
async def test_capture_form_valid_key_records_and_303s(mongo_db, capture_app):
    """A valid urlencoded native-form POST records the lead through the SAME
    capture pipeline (mapping applied, paw_* control fields stripped) and 303s to
    the validated Origin + the relative paw_redirect."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        # httpx ``data=`` sends application/x-www-form-urlencoded — the exact
        # content type a native <form method=post> submits.
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "https://brightsmiledental.com/thanks.html"

    leads = await leads_service.list_for_site(site.workspace, "site_form")
    assert len(leads) == 1
    assert leads[0].properties["full_name"] == "Sam Smiles"
    assert leads[0].properties["email"] == "sam@example.com"
    # The paw_* control fields never become lead properties.
    assert not any(k.startswith("paw_") for k in leads[0].properties)


@pytest.mark.asyncio
async def test_capture_form_invalid_key_is_401(mongo_db, capture_app):
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_key="WRONG"),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 401
    assert await leads_service.count_for_site(site.workspace, "site_form") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_redirect",
    ["https://evil.example.com/phish", "//evil.example.com", "/\\evil", "javascript://alert(1)"],
)
async def test_capture_form_non_relative_redirect_is_400(mongo_db, capture_app, bad_redirect):
    """Open-redirect guard: an absolute / protocol-relative / backslash / scheme'd
    paw_redirect is rejected BEFORE anything is recorded."""
    from pocketpaw_ee.cloud.leads import service as leads_service

    site = await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_redirect=bad_redirect),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 400, resp.text
    assert await leads_service.count_for_site(site.workspace, "site_form") == 0


@pytest.mark.asyncio
async def test_capture_form_wrong_origin_is_403(mongo_db, capture_app):
    await _form_site()
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(),
            headers={"origin": "https://evil.example.com"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_capture_form_unknown_site_is_404(mongo_db, capture_app):
    async with AsyncClient(transport=ASGITransport(app=capture_app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/capture/form",
            data=_form_fields(paw_site_id="site_missing"),
            headers={"origin": "https://brightsmiledental.com"},
        )
    assert resp.status_code == 404
