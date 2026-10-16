# tests/cloud/leads/test_connected_capture.py — lead intake for a CONNECTED site
# (``Site.foreign_origin``: the owner's own website running only the Paw Bar, no
# script_name). Covers the public JSON + form capture keyed by the site's object
# id, the origin gate with enforce on (verified host and owner extra allowed, an
# unproved allowed_origins entry and a stranger denied), the verified hosts in
# ``derived_origins``, the 303 base (verified host, never the caller's Origin),
# ``lead.captured`` firing with the object id, the settings view shape, and a
# revoked key (``Site.revoked``, the embed-key kill switch) refused on both paths.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.leads import service as leads_service
from pocketpaw_ee.cloud.leads.router import router
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.cloud.shared.events import event_bus

from pocketpaw.sites_capture.contact_form import CONTACT_FORM_TYPE, default_event_mapping
from tests.cloud.conftest import override_workspace_role

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "ws-connected"
KEY = "site_key_connected"


@pytest.fixture(autouse=True)
def _dev_posture(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.setenv("PAW_CAPTURE_API_BASE", "https://api.paw.test/api/v1/")


@pytest.fixture
def lead_events():
    seen: list[dict] = []

    async def _record(data: dict) -> None:
        seen.append(data)

    event_bus.subscribe("lead.captured", _record)
    yield seen
    event_bus.unsubscribe("lead.captured", _record)


async def _claim(host: str, *, status: str = "verified", age_days: int = 1, ws: str = WS) -> None:
    now = datetime.now(UTC)
    await SiteOriginClaim(
        workspace=ws,
        host=host,
        token="t",
        status=status,
        issued_at=now - timedelta(days=age_days),
        expires_at=now + timedelta(days=1),
        verified_at=(now - timedelta(days=age_days)) if status == "verified" else None,
        method="well-known" if status == "verified" else "",
    ).insert()


async def _connected(**kw) -> Site:
    """A connected site on shop.example.com (verified) that also lists
    staging.example.com, which this workspace never proved."""
    await _claim("shop.example.com")
    await _claim("staging.example.com", status="pending")
    fields = {
        "workspace": WS,
        "pocket_id": "pk-connected",
        "owner": "u1",
        "name": "Example Shop",
        "script_name": "",
        "url": "",
        "foreign_origin": True,
        "allowed_origins": ["shop.example.com", "staging.example.com"],
        "signed_key": KEY,
        "event_mapping": default_event_mapping(),
    }
    fields.update(kw)
    site = Site(**fields)
    await site.insert()
    return site


def _app(role: str = "admin") -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    override_workspace_role(app, role=role, workspace_id=WS, user_id="u1")
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _capture(app, site_id: str, origin: str | None, key: str = KEY):
    headers = {"origin": origin} if origin else {}
    async with _client(app) as c:
        return await c.post(
            f"/api/v1/sites/{site_id}/capture",
            json={
                "form_type": CONTACT_FORM_TYPE,
                "payload": {"name": "Sam", "email": "sam@example.com"},
                "signed_key": key,
            },
            headers=headers,
        )


async def _form(app, site_id: str, origin: str | None, redirect: str = "/thanks"):
    headers = {"origin": origin} if origin else {}
    async with _client(app) as c:
        return await c.post(
            "/api/v1/capture/form",
            data={
                "paw_site_id": site_id,
                "paw_key": KEY,
                "paw_form_type": CONTACT_FORM_TYPE,
                "paw_redirect": redirect,
                "name": "Sam",
                "email": "sam@example.com",
            },
            headers=headers,
        )


async def test_json_capture_files_the_lead_under_the_object_id_and_fires_lead_captured(
    lead_events,
) -> None:
    site = await _connected()
    resp = await _capture(_app(), str(site.id), "https://shop.example.com")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    leads = await leads_service.list_for_site(WS, str(site.id))
    assert len(leads) == 1
    assert leads[0].origin_unrecognized is False
    assert lead_events == [
        {
            "workspace_id": WS,
            "lead_id": leads[0].id,
            "site_id": str(site.id),
            "site_name": "Example Shop",
            "form_type": CONTACT_FORM_TYPE,
            "source_kind": "form",
        }
    ]
    # The notification sinks load the lead by this id and find the site.
    payload = await leads_service.lead_payload(WS, leads[0].id)
    assert payload["site_id"] == str(site.id)
    assert payload["site_name"] == "Example Shop"


async def test_a_revoked_key_captures_nothing_on_either_path() -> None:
    site = await _connected(revoked=True)
    app = _app()
    json_resp = await _capture(app, str(site.id), "https://shop.example.com")
    assert json_resp.status_code == 401
    form_resp = await _form(app, str(site.id), "https://shop.example.com")
    assert form_resp.status_code == 401
    assert await leads_service.list_for_site(WS, str(site.id)) == []


async def test_an_unproved_listed_host_is_flagged_unrecognized_by_default() -> None:
    site = await _connected()
    resp = await _capture(_app(), str(site.id), "https://staging.example.com")
    assert resp.status_code == 200
    [lead] = await leads_service.list_for_site(WS, str(site.id))
    assert lead.origin_unrecognized is True


async def test_form_capture_303s_to_the_verified_host_not_the_callers_origin(lead_events) -> None:
    site = await _connected()
    app = _app()

    on_site = await _form(app, str(site.id), "https://shop.example.com")
    assert on_site.status_code == 303
    assert on_site.headers["location"] == "https://shop.example.com/thanks"

    # A forged or unrecognized Origin never becomes the Location: the verified host does.
    forged = await _form(app, str(site.id), "https://evil.test")
    assert forged.status_code == 303
    assert forged.headers["location"] == "https://shop.example.com/thanks"

    absent = await _form(app, str(site.id), None)
    assert absent.headers["location"] == "https://shop.example.com/thanks"

    # The relative-path guard still runs first.
    bad = await _form(app, str(site.id), "https://shop.example.com", redirect="//evil.test/x")
    assert bad.status_code == 400

    assert len(await leads_service.list_for_site(WS, str(site.id))) == 3
    assert {e["site_id"] for e in lead_events} == {str(site.id)}


async def test_a_connected_site_with_no_verified_host_redirects_relative() -> None:
    site = await _connected(allowed_origins=["staging.example.com"])
    resp = await _form(_app(), str(site.id), "https://evil.test")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/thanks"


async def test_enforced_capture_allows_verified_and_extra_hosts_and_denies_the_rest() -> None:
    site = await _connected()
    app = _app()
    async with _client(app) as c:
        put = await c.put(
            f"/api/v1/sites/{site.id}/lead-intake",
            json={"extra_origins": ["landing.example.org"], "enforce_origin": True},
        )
    assert put.status_code == 200, put.text

    assert (await _capture(app, str(site.id), "https://shop.example.com")).status_code == 200
    assert (await _capture(app, str(site.id), "https://landing.example.org")).status_code == 200
    assert (await _capture(app, str(site.id), "https://staging.example.com")).status_code == 403
    assert (await _capture(app, str(site.id), "https://evil.test")).status_code == 403
    assert (await _capture(app, str(site.id), None)).status_code == 403
    assert (await _form(app, str(site.id), "https://evil.test")).status_code == 403

    form_ok = await _form(app, str(site.id), "https://landing.example.org")
    assert form_ok.headers["location"] == "https://landing.example.org/thanks"


async def test_an_old_verified_claim_still_counts_as_the_sites_own_host() -> None:
    await _claim("old.example.com", age_days=400)
    site = await _connected(allowed_origins=["old.example.com"], enforce_origin=True)
    assert (await _capture(_app(), str(site.id), "https://old.example.com")).status_code == 200


async def test_capture_refuses_a_bad_key_and_an_unknown_or_draft_site() -> None:
    site = await _connected()
    draft = Site(workspace=WS, pocket_id="pk-draft", owner="u1", signed_key=KEY)
    await draft.insert()
    app = _app()
    assert (await _capture(app, str(site.id), None, key="wrong")).status_code == 401
    assert (await _capture(app, str(draft.id), None)).status_code == 404
    assert (await _capture(app, "0" * 24, None)).status_code == 404
    assert (await _form(app, str(draft.id), None)).status_code == 404


async def test_settings_view_for_a_connected_site() -> None:
    site = await _connected(lead_intake_origins=["landing.example.org"], honeypot_field="hp_trap")
    async with _client(_app()) as c:
        resp = await c.get(f"/api/v1/sites/{site.id}/lead-intake")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": str(site.id),
        "site_kind": "connected",
        "signed_key": KEY,
        "capture_url": f"https://api.paw.test/api/v1/sites/{site.id}/capture",
        "form_url": "https://api.paw.test/api/v1/capture/form",
        "allowed_origins": ["shop.example.com", "landing.example.org"],
        "derived_origins": ["shop.example.com"],
        "extra_origins": ["landing.example.org"],
        "enforce_origin": False,
        "max_extra_origins": 20,
        "honeypot_field": "hp_trap",
    }


async def test_settings_view_mints_a_key_for_a_keyless_connected_site() -> None:
    site = await _connected(signed_key="")
    async with _client(_app()) as c:
        first = (await c.get(f"/api/v1/sites/{site.id}/lead-intake")).json()
        second = (await c.get(f"/api/v1/sites/{site.id}/lead-intake")).json()
    assert first["signed_key"].startswith("site_key_")
    assert second["signed_key"] == first["signed_key"]
    assert (await Site.get(site.id)).signed_key == first["signed_key"]


async def test_another_workspaces_verified_claim_does_not_count() -> None:
    await _claim("theirs.example.com", ws="ws-other")
    site = await _connected(allowed_origins=["theirs.example.com"])
    async with _client(_app()) as c:
        body = (await c.get(f"/api/v1/sites/{site.id}/lead-intake")).json()
    assert body["derived_origins"] == []
