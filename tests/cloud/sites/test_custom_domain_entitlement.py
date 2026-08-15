# tests/cloud/sites/test_custom_domain_entitlement.py — a custom domain is a PAID
# per-site capability, and until this branch nothing checked whether the site was
# entitled to one.
#
# Created 2026-08-15 (fix/sites-custom-domain-entitlement). The hole: ``add_domain``
# already resolved the site's plan — but only to pass ``cloudflare_features`` to
# Cloudflare (BC-10 resale). No branch anywhere asked "may this site have a custom
# domain at all". The only gate on the endpoint was RBAC (``fabric.write``), which
# is permission, not billing. So a basic-tier site attached a custom domain and
# kept it, and so did a paid site whose subscription was cancelled or never
# charged.
#
# ``resolve_site_entitlements`` has answered this since 2026-08-13 and had exactly
# ONE caller (the badge stamper). This tree pins the second one.
#
# Deliberately tier-key agnostic: every criterion reads the tier that grants
# ``custom_domain`` out of the catalog rather than hardcoding "pro", so the
# pricing-spec rekey (basic/pro/business → free/site/staff) moves these tests with
# the catalog instead of breaking them.
#
# Two postures inherited from the siblings this gate copies (``PocketLimitError``,
# ``ConnectorLimitError``) and pinned here because both are easy to "fix" wrongly
# later:
#   * ATTACH-TIME ONLY, never retroactive — a downgrade does not rip a live domain
#     off a deployed site mid-flight, and the re-Add route REPAIR stays reachable.
#     The spec's downgrade rules detach at period end; that is a different lane.
#   * Gated on ``billing_enforced`` — OSS / self-host has no billing and must not
#     acquire a paywall from this branch.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site, SiteDomain  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

pytestmark = pytest.mark.usefixtures("mongo_db")


# --------------------------------------------------------------------------- #
# Catalog-derived tier keys — see the header on why these are not literals.
# --------------------------------------------------------------------------- #


def _a_tier_granting_custom_domain() -> str:
    """The cheapest catalog tier that resells ``custom_domain``."""
    for tier in site_plans.list_site_plans():
        if "custom_domain" in tier.cloudflare_features:
            return tier.key
    raise AssertionError("no site plan tier resells custom_domain — catalog changed")


def _the_free_tier() -> str:
    """The base/floor tier. It resells nothing, which is what free means."""
    return site_plans.BASE_SITE_PLAN_KEY


# --------------------------------------------------------------------------- #
# Test doubles.
# --------------------------------------------------------------------------- #


class _RecordingCF:
    """Stand-in CloudflareClient that records calls and never touches the network.

    Records ``create_custom_hostname`` AND ``create_worker_route`` because one
    criterion here is specifically that a refused attach reaches NEITHER — a gate
    that raises after Cloudflare has already accepted the hostname leaves an orphan
    on the zone that nothing in the product can see or clear.
    """

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.route_calls: list[dict] = []
        self.delete_calls: list[str] = []

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

    async def create_worker_route(self, *, pattern: str, script: str) -> str:
        self.route_calls.append({"pattern": pattern, "script": script})
        return "route_test"

    async def delete_custom_hostname(self, hostname_id: str) -> None:
        self.delete_calls.append(hostname_id)


def _enforce(monkeypatch, *, on: bool) -> None:
    """Point the config's ``get_settings`` at a flag stub carrying the billing
    posture. Lazily imported inside the gate, same as the connector/pocket caps."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_site_products=None),
    )


async def _seed_site(
    *,
    workspace_id: str,
    plan_tier: str | None,
    subscription_status: str = "none",
    deployed: bool = True,
) -> str:
    """Insert a real Site doc and return its id string.

    ``deployed=True`` by default: an unpublished site is refused by a DIFFERENT
    guard (``sites.domain_needs_publish``), and a test that tripped that one while
    believing it proved the entitlement gate would pass for the wrong reason.
    """
    doc = Site(
        workspace=workspace_id,
        pocket_id="pk_1",
        owner="u1",
        name="My Site",
        plan_tier=plan_tier,
        subscription_status=subscription_status,
        deployed=deployed,
    )
    await doc.insert()
    return str(doc.id)


# --------------------------------------------------------------------------- #
# The bug: a site with no paid entitlement attached a custom domain.
# --------------------------------------------------------------------------- #


async def test_a_free_site_cannot_attach_a_custom_domain(monkeypatch):
    """The headline case. A base-tier site resells no custom_domain, so the attach
    is refused with a 402 the UI can turn into an upgrade prompt."""
    _enforce(monkeypatch, on=True)
    ws = "ws_free_domain"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.freeloader.com",
            _cloudflare=cf,
        )

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.custom_domain_not_entitled"


async def test_an_unset_tier_cannot_attach_a_custom_domain(monkeypatch):
    """A site with no ``plan_tier`` at all (pre-BC-9 rows, and every first publish)
    resolves to the floor. Fail-closed: absent is free, not exempt."""
    _enforce(monkeypatch, on=True)
    ws = "ws_untiered_domain"
    site_id = await _seed_site(workspace_id=ws, plan_tier=None)
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.untiered.com",
            _cloudflare=cf,
        )

    assert exc.value.status_code == 402


async def test_a_refused_attach_never_reaches_cloudflare(monkeypatch):
    """The gate runs BEFORE any Cloudflare call.

    Raising after ``create_custom_hostname`` succeeds would leave a hostname on the
    shared zone with no Site row pointing at it — invisible to the product, and it
    makes the customer's next legitimate attach fail on a 1406 duplicate they
    cannot see or clear.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_no_cf_call"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.nocall.com",
            _cloudflare=cf,
        )

    assert cf.create_calls == []
    assert cf.route_calls == []


async def test_a_refused_attach_writes_nothing_to_the_site(monkeypatch):
    """No half-state: the refused hostname is not on ``domains``, and — the easier
    one to miss — not on ``allowed_origins`` either, which is what authorizes a
    host to POST captures at the site."""
    _enforce(monkeypatch, on=True)
    ws = "ws_no_write"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.nowrite.com",
            _cloudflare=cf,
        )

    doc = await Site.get(site_id)
    assert doc.domains == []
    assert "www.nowrite.com" not in doc.allowed_origins


# --------------------------------------------------------------------------- #
# The wider half of the hole: a paid TIER without a paying SUBSCRIPTION.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["none", "pending", "cancelled"])
async def test_a_paid_tier_without_an_active_subscription_is_refused(monkeypatch, status):
    """Cancellation never resets ``plan_tier``, and an unconfigured Dodo product
    records a paid tier with no charge at all. Reading the tier alone therefore
    hands a free custom domain to sites that have never paid — permanently. The
    resolver gates on tier AND subscription; this proves the attach seam does too.
    """
    _enforce(monkeypatch, on=True)
    ws = f"ws_paid_{status}"
    site_id = await _seed_site(
        workspace_id=ws,
        plan_tier=_a_tier_granting_custom_domain(),
        subscription_status=status,
    )
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname=f"www.{status}.com",
            _cloudflare=cf,
        )

    assert exc.value.status_code == 402
    assert cf.create_calls == []


# --------------------------------------------------------------------------- #
# The paying case still works — the gate must not become the bug.
# --------------------------------------------------------------------------- #


async def test_an_active_paid_site_attaches_normally(monkeypatch):
    """A tier that resells custom_domain, on an active subscription, is unaffected:
    Cloudflare is called and the domain lands on the site."""
    _enforce(monkeypatch, on=True)
    ws = "ws_paid_active"
    tier = _a_tier_granting_custom_domain()
    site_id = await _seed_site(workspace_id=ws, plan_tier=tier, subscription_status="active")
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.paying.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.paying.com"
    assert len(cf.create_calls) == 1
    doc = await Site.get(site_id)
    assert [d.hostname for d in doc.domains] == ["www.paying.com"]


async def test_the_resold_feature_set_still_rides_along(monkeypatch):
    """The BC-10 resale contract is untouched by the gate — an entitled attach still
    passes its tier's ``cloudflare_features`` through to provisioning."""
    _enforce(monkeypatch, on=True)
    ws = "ws_features_intact"
    tier = _a_tier_granting_custom_domain()
    site_id = await _seed_site(workspace_id=ws, plan_tier=tier, subscription_status="active")
    cf = _RecordingCF()

    await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.features.com",
        _cloudflare=cf,
    )

    assert cf.create_calls[0]["features"] == set(site_plans.get_site_plan(tier).cloudflare_features)


# --------------------------------------------------------------------------- #
# Never retroactive — the posture inherited from the pocket/connector caps.
# --------------------------------------------------------------------------- #


async def test_an_already_connected_domain_still_repairs_its_route(monkeypatch):
    """A site that LOST its entitlement keeps its live domain, and the re-Add route
    repair stays reachable.

    Pressing Add again is the only self-service fix for a domain connected before
    the routing lane shipped (those have no route and silently serve the fallback
    origin while Cloudflare reports them active). If the gate ran before the repair,
    a downgraded site would be stuck serving a broken domain with no way out — and
    detaching on downgrade is a period-end job, not this seam's.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_repair"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    doc = await Site.get(site_id)
    # ``_route_target`` asks the SITE what was deployed, never the environment what
    # is configured — and this tree's conftest clears the operator's PAW_CF_* env,
    # so the pre-field fallback resolves to "". Stamping the target is what gives
    # this site a route-addressable Worker, and therefore a route to repair.
    doc.deploy_target = "workers"
    doc.domains.append(
        SiteDomain(
            hostname="www.legacy.com",
            cf_hostname_id="ch_legacy",
            cname_target="zone_1.cdn.cloudflare.net",
            status="active",
            cf_route_id="",  # the missing route this call exists to repair
        )
    )
    await doc.save()
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.legacy.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.legacy.com"
    assert len(cf.route_calls) == 1
    refreshed = await Site.get(site_id)
    assert refreshed.domains[0].cf_route_id == "route_test"


# --------------------------------------------------------------------------- #
# OSS / self-host — billing off means no paywall.
# --------------------------------------------------------------------------- #


async def test_with_billing_off_the_gate_never_fires(monkeypatch):
    """With ``billing_enforced`` off, a free site attaches a custom domain exactly
    as it did before this branch. Self-host has no billing and must not inherit a
    paywall from it."""
    _enforce(monkeypatch, on=False)
    ws = "ws_oss"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.selfhost.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.selfhost.com"
    assert len(cf.create_calls) == 1
