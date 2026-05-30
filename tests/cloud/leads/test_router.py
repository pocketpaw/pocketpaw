# tests/cloud/leads/test_router.py — exercises capture hardening at the router
# (origin pinning + signed key live here) plus the authed read. Pattern: build
# an app that mounts the leads router; for the public endpoint, no auth needed;
# for the read endpoint, follow the existing cloud router test pattern for
# injecting an authed user + active workspace.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.4): router-level
# tests for the public capture surface — wrong-origin reject, bad-signed-key
# reject, and the happy-path accept.
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
