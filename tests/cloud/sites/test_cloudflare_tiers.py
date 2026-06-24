# tests/cloud/sites/test_cloudflare_tiers.py — proves the BC-10 contract: resell
# Cloudflare features by site-plan tier + expose the per-site plan catalog.
#
#   1. Adding a domain to a site whose plan_tier resells WAF/security features →
#      add_domain resolves the tier's cloudflare_features and calls
#      create_custom_hostname WITH that set.
#   2. A BASIC-tier site (empty cloudflare_features) → create_custom_hostname is
#      called WITHOUT premium settings (the unchanged basic ssl payload), proven
#      end to end against the real CloudflareClient + a mocked transport.
#   3. The tier → feature mapping is honored exactly (the features passed per tier
#      match site_plans, and the CF request carries the premium ssl.settings only
#      for tiers that resell them).
#   4. GET /billing/site-plans returns the catalog tiers with their
#      cloudflare_features.
#
# add_domain loads a REAL seeded Site doc (it owns Site writes), so the
# tier-resolution criteria run against live Beanie with a fake CF client that
# records the ``features`` kwarg. The wire-shape criterion (premium ssl in the CF
# request body) drives the real CloudflareClient with an httpx.MockTransport, the
# same no-network pattern as test_cloudflare_client.py. The HTTP catalog read uses
# a FastAPI TestClient with require_license waived (same pattern as test_plans.py).
#
# Created 2026-06-24 (integration/billing-credits, BC-10): new test module.

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.router import router as billing_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class _RecordingCF:
    """Stand-in CloudflareClient that records the ``features`` kwarg add_domain
    passes to create_custom_hostname (never touches the network)."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []

    async def create_custom_hostname(
        self, hostname: str, *, features: set[str] | None = None
    ) -> CustomHostname:
        self.create_calls.append({"hostname": hostname, "features": features})
        return CustomHostname(
            id="ch_test",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )


async def _seed_site(*, workspace_id: str, plan_tier: str | None) -> str:
    """Insert a real Site doc on ``plan_tier`` and return its id string."""
    doc = Site(
        workspace=workspace_id,
        pocket_id="pk_1",
        owner="u1",
        name="My Site",
        plan_tier=plan_tier,
    )
    await doc.insert()
    return str(doc.id)


# ---------------------------------------------------------------------------
# Criterion 1 — a higher tier provisions its resold features.
# ---------------------------------------------------------------------------


async def test_add_domain_passes_business_tier_features(mongo_db):
    """A business-tier site (resells WAF + edge_cache) → create_custom_hostname
    is called WITH that feature set."""
    ws = "ws_business"
    site_id = await _seed_site(workspace_id=ws, plan_tier="business")
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.example.com",
        _cloudflare=cf,
    )

    assert len(cf.create_calls) == 1
    passed = cf.create_calls[0]["features"]
    expected = site_plans.get_site_plan("business").cloudflare_features
    assert passed == set(expected)
    # The premium security features ride along.
    assert "waf" in passed
    assert "edge_cache" in passed


# ---------------------------------------------------------------------------
# Criterion 2 — a basic tier provisions nothing premium (current behavior).
# ---------------------------------------------------------------------------


async def test_add_domain_basic_tier_passes_no_features(mongo_db):
    """A basic-tier site resells no Cloudflare features → create_custom_hostname is
    called with an EMPTY feature set (the basic path)."""
    ws = "ws_basic"
    site_id = await _seed_site(workspace_id=ws, plan_tier="basic")
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.basic.com",
        _cloudflare=cf,
    )

    assert cf.create_calls[0]["features"] == set()


async def test_add_domain_unset_tier_passes_no_features(mongo_db):
    """A site with no plan_tier (pre-BC-9 / workspace-plan-only) resolves to no
    features — never an error, never premium provisioning."""
    ws = "ws_none"
    site_id = await _seed_site(workspace_id=ws, plan_tier=None)
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.none.com",
        _cloudflare=cf,
    )

    assert cf.create_calls[0]["features"] == set()


# ---------------------------------------------------------------------------
# Criterion 3 — the tier → feature mapping is honored exactly, end to end.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["basic", "pro", "business"])
async def test_add_domain_honors_exact_tier_feature_mapping(mongo_db, tier):
    """For every tier the features add_domain passes equal that tier's catalog
    cloudflare_features — no more, no less."""
    ws = f"ws_{tier}"
    site_id = await _seed_site(workspace_id=ws, plan_tier=tier)
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname=f"www.{tier}.com",
        _cloudflare=cf,
    )

    assert cf.create_calls[0]["features"] == set(site_plans.get_site_plan(tier).cloudflare_features)


def _cf_client(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )


def _hostname_ok_response(request: httpx.Request, seen: dict) -> httpx.Response:
    import json

    seen["body"] = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": {
                "id": "ch_1",
                "hostname": "www.example.com",
                "status": "pending",
                "ssl": {"status": "pending_validation"},
            },
        },
    )


@pytest.mark.asyncio
async def test_create_custom_hostname_premium_features_inject_ssl_settings():
    """With premium features the CF request body carries an ``ssl.settings`` block
    (strict TLS for waf, http2 for edge_cache) + records the resold feature set."""
    seen: dict = {}
    client = _cf_client(lambda req: _hostname_ok_response(req, seen))

    await client.create_custom_hostname(
        "www.example.com", features={"custom_domain", "analytics", "waf", "edge_cache"}
    )

    ssl = seen["body"]["ssl"]
    # Base DV fields preserved.
    assert ssl["method"] == "http"
    assert ssl["type"] == "dv"
    # Premium toggles for waf + edge_cache present.
    assert ssl["settings"]["min_tls_version"] == "1.2"
    assert ssl["settings"]["tls_1_3"] == "on"
    assert ssl["settings"]["http2"] == "on"
    # The resold set is recorded on the hostname (sorted, deterministic).
    assert ssl["custom_metadata"]["resold_features"] == ("analytics,custom_domain,edge_cache,waf")


@pytest.mark.asyncio
async def test_create_custom_hostname_no_features_is_basic_unchanged():
    """No features → the request body is the prior basic DV payload, byte-for-byte
    (no ssl.settings, no custom_metadata) — the basic path never regresses."""
    seen: dict = {}
    client = _cf_client(lambda req: _hostname_ok_response(req, seen))

    await client.create_custom_hostname("www.example.com")

    assert seen["body"]["ssl"] == {"method": "http", "type": "dv"}


# ---------------------------------------------------------------------------
# Criterion 4 — GET /billing/site-plans returns the catalog.
# ---------------------------------------------------------------------------


@pytest.fixture
def site_plans_client() -> TestClient:
    """A TestClient over an app mounting the billing router, license waived."""
    app = FastAPI()
    app.include_router(billing_router)
    app.dependency_overrides[require_license] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_get_billing_site_plans_returns_catalog_with_cf_features(site_plans_client):
    resp = site_plans_client.get("/billing/site-plans")
    assert resp.status_code == 200
    body = resp.json()
    rows = {row["key"]: row for row in body["site_plans"]}
    assert set(rows.keys()) == {"basic", "pro", "business"}

    # Each tier carries its annual price + sorted cloudflare_features.
    assert rows["basic"]["annual_price_usd"] == 0
    assert rows["basic"]["cloudflare_features"] == []
    assert "custom_domain" in rows["pro"]["cloudflare_features"]
    biz_features = rows["business"]["cloudflare_features"]
    assert "waf" in biz_features
    assert "edge_cache" in biz_features
    # Features arrive as a SORTED JSON array (deterministic wire payload).
    assert biz_features == sorted(biz_features)
    # The catalog matches the source-of-truth site_plans exactly.
    assert set(biz_features) == set(site_plans.get_site_plan("business").cloudflare_features)
