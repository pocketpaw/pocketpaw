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
    site_id = await _seed_site(workspace_id=ws, plan_tier="staff")
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.example.com",
        _cloudflare=cf,
    )

    assert len(cf.create_calls) == 1
    passed = cf.create_calls[0]["features"]
    expected = site_plans.get_site_plan("staff").cloudflare_features
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
    site_id = await _seed_site(workspace_id=ws, plan_tier="free")
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


@pytest.mark.parametrize("org_key", ["studio", "agency"])
async def test_add_domain_provisions_nothing_for_an_org_flat_stored_on_a_site(mongo_db, org_key):
    """An ORG key on a site's ``plan_tier`` provisions no Cloudflare features.

    ``studio`` and ``agency`` are real catalog rows and they DO resell the full
    Cloudflare set — that is the point of the test. A plain ``get_site_plan``
    here resolves one and provisions the WAF and edge-cache controls an org buys
    across its whole estate onto a single site nobody billed for them, and
    Cloudflare charges us for it. ``site_scoped_tier`` returns None, which is the
    same answer an absent tier gets: nothing premium.

    Breaks on: ``add_domain`` going back to ``site_plans.get_site_plan``.
    """
    assert site_plans.get_site_plan(org_key).cloudflare_features, (
        "if the org tiers stopped reselling anything this test would pass for the wrong reason"
    )

    ws = f"ws_{org_key}"
    site_id = await _seed_site(workspace_id=ws, plan_tier=org_key)
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname=f"www.{org_key}.com",
        _cloudflare=cf,
    )

    assert cf.create_calls[0]["features"] == set()


# ---------------------------------------------------------------------------
# Criterion 3 — the tier → feature mapping is honored exactly, end to end.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["free", "site", "staff"])
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
        # Required since 2026-08-12: the target is the one thing a customer pastes, so
        # an unconfigured one is refused rather than derived into a name with no DNS
        # records. Any value does here — these tests are about the ssl payload.
        cname_target="sites.pawzone.test",
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
    (strict TLS for waf, http2 for edge_cache).

    The ``custom_metadata`` half of BC-10 is GONE, and this test used to require it.
    Per-hostname metadata is not generally available — Cloudflare's own doc says "only
    certain customers have access to this feature… contact your account team" — so
    sending it 403s (1413) on an ordinary zone. The effect in production was that
    custom domains worked on FREE sites and failed on PAID ones, because only a tier
    with ``cloudflare_features`` attached the block. The 2026-06-25 resale research had
    already marked these SKUs DEFER; asserting the block here is what kept the
    contradiction alive. The entitlement-free ``ssl.settings`` half is unchanged and is
    still checked exactly.
    """
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
    # And nothing entitlement-gated rides along, on any tier.
    assert "custom_metadata" not in ssl


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
    # The whole catalog, both scopes — the storefront renders the org flats beside
    # the per-site rungs, so this endpoint must not filter them out.
    assert set(rows.keys()) == {"free", "site", "staff", "studio", "agency"}
    # ...and each row says WHICH it is, because an org flat's key is not a legal
    # ``site_plan_key`` on a publish and the client needs to know that from the
    # payload rather than from a hardcoded list of two names.
    assert rows["site"]["scope"] == "site"
    assert rows["studio"]["scope"] == "org"

    # Each tier carries its monthly price + sorted cloudflare_features.
    assert rows["free"]["monthly_price_usd"] == 0
    assert rows["free"]["cloudflare_features"] == []
    assert "custom_domain" in rows["site"]["cloudflare_features"]
    biz_features = rows["staff"]["cloudflare_features"]
    assert "waf" in biz_features
    assert "edge_cache" in biz_features
    # Features arrive as a SORTED JSON array (deterministic wire payload).
    assert biz_features == sorted(biz_features)
    # The catalog matches the source-of-truth site_plans exactly.
    assert set(biz_features) == set(site_plans.get_site_plan("staff").cloudflare_features)
