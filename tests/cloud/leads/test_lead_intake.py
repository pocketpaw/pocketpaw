# tests/cloud/leads/test_lead_intake.py — the Collect leads settings
# (GET/PUT /sites/{site_id}/lead-intake, ``cloud/leads/intake.py``): origin
# normalization and validation, the 20-host cap, the admin gate (member 403,
# another workspace 404), the GET/PUT round trip, the derived hosts an owner
# cannot remove, and that the capture gate honours owner-added hosts once the
# site turns on ``enforce_origin``.
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.leads import intake
from pocketpaw_ee.cloud.leads.router import router
from pocketpaw_ee.cloud.models.site import Site, SiteDomain

from pocketpaw.sites_capture.contact_form import CONTACT_FORM_TYPE, default_event_mapping
from tests.cloud.conftest import override_workspace_role

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "ws-intake"


@pytest.fixture(autouse=True)
def _dev_posture(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("PAW_CAPTURE_API_BASE", "https://api.paw.test/api/v1/")


async def _site(ws: str = WS, script: str = "site-bright", **kw) -> Site:
    fields = {
        "workspace": ws,
        "pocket_id": f"pk-{script}",
        "owner": "u1",
        "script_name": script,
        "signed_key": "key_ok",
        "url": "https://bright.pages.dev",
        "allowed_origins": ["localhost", "bright.pages.dev"],
        "domains": [SiteDomain(hostname="brightsmile.com", status="live")],
        "event_mapping": default_event_mapping(),
    }
    fields.update(kw)
    site = Site(**fields)
    await site.insert()
    return site


def _app(role: str = "admin", ws: str = WS) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    override_workspace_role(app, role=role, workspace_id=ws, user_id="u1")
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "host"),
    [
        ("https://Shop.Example.com:8443/contact?x=1#top", "shop.example.com"),
        ("shop.example.com.", "shop.example.com"),
        ("  WWW.Example.CO.UK ", "www.example.co.uk"),
        ("http://user:pw@example.org/", "example.org"),
        ("localhost:5173", "localhost"),
    ],
)
def test_normalize_reduces_to_a_bare_hostname(raw, host) -> None:
    assert intake.normalize_origin(raw) == host


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "*.example.com",
        "*",
        "10.0.0.5",
        "https://93.184.216.34/form",
        "[::1]",
        "intranet",
        "exa mple.com",
        "-bad-.example.com",
        "under_score.example.com",
    ],
)
def test_normalize_rejects_ips_wildcards_and_bad_names(raw) -> None:
    with pytest.raises(ValidationError):
        intake.normalize_origin(raw)


def test_localhost_is_refused_in_a_production_posture(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    with pytest.raises(ValidationError):
        intake.normalize_origin("http://localhost:3000")


def test_dedupe_keeps_first_spelling_and_order() -> None:
    hosts = intake.normalize_origins(
        ["https://b.example.com", "a.example.com", "B.example.com/x", "a.example.com:80"]
    )
    assert hosts == ["b.example.com", "a.example.com"]


def test_cap_counts_distinct_hosts() -> None:
    twenty = [f"h{i}.example.com" for i in range(20)]
    assert len(intake.normalize_origins(twenty + twenty)) == 20
    with pytest.raises(ValidationError) as exc:
        intake.normalize_origins([*twenty, "h20.example.com"])
    assert exc.value.code == "leads.intake_origins_too_many"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def test_get_returns_key_absolute_urls_and_origin_sets() -> None:
    site = await _site()
    async with _client(_app()) as c:
        resp = await c.get(f"/api/v1/sites/{site.id}/lead-intake")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": "site-bright",
        "signed_key": "key_ok",
        "capture_url": "https://api.paw.test/api/v1/sites/site-bright/capture",
        "form_url": "https://api.paw.test/api/v1/capture/form",
        "allowed_origins": ["localhost", "bright.pages.dev", "brightsmile.com"],
        "derived_origins": ["localhost", "bright.pages.dev", "brightsmile.com"],
        "extra_origins": [],
        "enforce_origin": False,
        "max_extra_origins": 20,
    }


async def test_put_round_trips_and_merges_with_derived_hosts() -> None:
    site = await _site()
    async with _client(_app()) as c:
        put = await c.put(
            "/api/v1/sites/site-bright/lead-intake",
            json={
                "extra_origins": ["https://Landing.Example.com/form", "landing.example.com"],
                "enforce_origin": True,
            },
        )
        assert put.status_code == 200, put.text
        got = await c.get(f"/api/v1/sites/{site.id}/lead-intake")
    body = got.json()
    assert body == put.json()
    assert body["extra_origins"] == ["landing.example.com"]
    assert body["enforce_origin"] is True
    assert body["allowed_origins"] == [
        "localhost",
        "bright.pages.dev",
        "brightsmile.com",
        "landing.example.com",
    ]

    # The publish-stamped allowlist is untouched; extras live in their own field.
    fresh = await Site.get(site.id)
    assert fresh.allowed_origins == ["localhost", "bright.pages.dev"]
    assert fresh.lead_intake_origins == ["landing.example.com"]

    # Clearing the extras cannot remove a derived host.
    async with _client(_app()) as c:
        cleared = await c.put(
            "/api/v1/sites/site-bright/lead-intake",
            json={"extra_origins": [], "enforce_origin": True},
        )
    assert cleared.json()["allowed_origins"] == [
        "localhost",
        "bright.pages.dev",
        "brightsmile.com",
    ]


async def test_put_rejects_bad_input_without_writing() -> None:
    site = await _site()
    async with _client(_app()) as c:
        bad = await c.put(
            f"/api/v1/sites/{site.id}/lead-intake",
            json={"extra_origins": ["ok.example.com", "10.0.0.1"], "enforce_origin": True},
        )
        too_many = await c.put(
            f"/api/v1/sites/{site.id}/lead-intake",
            json={
                "extra_origins": [f"h{i}.example.com" for i in range(21)],
                "enforce_origin": False,
            },
        )
        partial = await c.put(f"/api/v1/sites/{site.id}/lead-intake", json={"extra_origins": []})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "leads.intake_origin_invalid"
    assert too_many.status_code == 422
    assert partial.status_code == 422
    fresh = await Site.get(site.id)
    assert fresh.lead_intake_origins == []
    assert fresh.enforce_origin is False


async def test_member_is_forbidden_and_another_workspace_is_404() -> None:
    site = await _site()
    foreign = await _site(ws="ws-other", script="site-other")
    async with _client(_app("member")) as member:
        assert (await member.get(f"/api/v1/sites/{site.id}/lead-intake")).status_code == 403
        put = await member.put(
            f"/api/v1/sites/{site.id}/lead-intake",
            json={"extra_origins": ["x.example.com"], "enforce_origin": True},
        )
        assert put.status_code == 403
    async with _client(_app()) as admin:
        assert (await admin.get(f"/api/v1/sites/{foreign.id}/lead-intake")).status_code == 404
        cross = await admin.put(
            "/api/v1/sites/site-other/lead-intake",
            json={"extra_origins": ["x.example.com"], "enforce_origin": True},
        )
        assert cross.status_code == 404
    assert (await Site.get(foreign.id)).lead_intake_origins == []


# ---------------------------------------------------------------------------
# Capture with the pin on
# ---------------------------------------------------------------------------


async def _capture(app: FastAPI, origin: str):
    async with _client(app) as c:
        return await c.post(
            "/api/v1/sites/site-bright/capture",
            json={
                "form_type": CONTACT_FORM_TYPE,
                "payload": {"name": "Sam", "email": "sam@example.com"},
                "signed_key": "key_ok",
            },
            headers={"origin": origin},
        )


async def _form(app: FastAPI, origin: str):
    async with _client(app) as c:
        return await c.post(
            "/api/v1/capture/form",
            data={
                "paw_site_id": "site-bright",
                "paw_key": "key_ok",
                "paw_form_type": CONTACT_FORM_TYPE,
                "name": "Sam",
                "email": "sam@example.com",
            },
            headers={"origin": origin},
        )


async def test_enforced_capture_accepts_an_added_origin_and_rejects_an_unknown_one() -> None:
    from pocketpaw_ee.cloud.leads import service as leads_service

    await _site()
    app = _app()
    async with _client(app) as c:
        put = await c.put(
            "/api/v1/sites/site-bright/lead-intake",
            json={"extra_origins": ["landing.example.com"], "enforce_origin": True},
        )
    assert put.status_code == 200, put.text

    ok = await _capture(app, "https://landing.example.com")
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    form_ok = await _form(app, "https://landing.example.com")
    assert form_ok.status_code == 303
    assert form_ok.headers["location"] == "https://landing.example.com/"

    assert (await _capture(app, "https://evil.example.com")).status_code == 403
    assert (await _form(app, "https://evil.example.com")).status_code == 403

    leads = await leads_service.list_for_site(WS, "site-bright")
    assert len(leads) == 2
    assert all(lead.origin_unrecognized is False for lead in leads)


async def test_enforced_capture_with_only_derived_hosts_accepts_the_sites_own_pages() -> None:
    """A row whose stamped allowlist never got the deployed host (draft publish,
    async build) still accepts its own url host and custom domain when the owner
    turns the pin on without adding anything."""
    await _site(allowed_origins=["localhost"])
    app = _app()
    async with _client(app) as c:
        put = await c.put(
            "/api/v1/sites/site-bright/lead-intake",
            json={"extra_origins": [], "enforce_origin": True},
        )
    assert put.status_code == 200
    assert (await _capture(app, "https://bright.pages.dev")).status_code == 200
    assert (await _capture(app, "https://brightsmile.com")).status_code == 200
    assert (await _capture(app, "https://elsewhere.example.com")).status_code == 403
