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
# Updated 2026-08-21 (feat/site-free-custom-domain, PW-1). Free now INCLUDES a
# custom domain — one site's worth — so the headline assertions here invert: a free
# site attaches its domain and the refusal moves to the SECOND site that wants one.
# That count gate has its own tree (test_custom_domain_cap.py); what stays here is
# the capability gate and the postures around it, which are unchanged.
#
# The lapsed-subscription cases invert too, and that one is worth reading twice: a
# paid site whose subscription stopped falls to the FLOOR, not to zero. It keeps
# what free would have given it and loses the uncapped allowance, which is what it
# was actually paying for. Refusing it outright would leave a former customer worse
# off than someone who never paid.
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
    """The cheapest catalog tier whose domain allowance is UNCAPPED.

    Since free includes a domained site, "grants a custom domain" no longer picks
    out a paid tier — every tier grants one. What a paid tier sells is the absence
    of a ceiling, so that is what this selects on. Still read off the catalog, never
    hardcoded, so the pricing-spec rekey moves it instead of breaking it.
    """
    for tier in site_plans.list_site_plans():
        if tier.max_domained_sites is None:
            return tier.key
    raise AssertionError("no site plan tier has an uncapped domain allowance — catalog changed")


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
    pocket_id: str = "pk_1",
) -> str:
    """Insert a real Site doc and return its id string.

    ``deployed=True`` by default: an unpublished site is refused by a DIFFERENT
    guard (``sites.domain_needs_publish``), and a test that tripped that one while
    believing it proved the entitlement gate would pass for the wrong reason.
    """
    doc = Site(
        workspace=workspace_id,
        pocket_id=pocket_id,
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


async def test_a_free_site_attaches_its_one_custom_domain(monkeypatch):
    """The captain's rule, end to end at the seam that enforces it.

    This assertion is the inverse of the one that stood here until 2026-08-21, and
    the inversion IS the feature: free includes a custom domain on one site. What
    free does not include is a second site with one, which is a count and lives in
    test_custom_domain_cap.py.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_free_domain"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.freeloader.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.freeloader.com"
    assert len(cf.create_calls) == 1
    doc = await Site.get(site_id)
    assert [d.hostname for d in doc.domains] == ["www.freeloader.com"]


async def test_an_unset_tier_gets_the_floor_allowance(monkeypatch):
    """A site with no ``plan_tier`` at all (pre-BC-9 rows, and every first publish)
    resolves to the floor — and the floor now carries a domain. Absent is free, and
    free includes one."""
    _enforce(monkeypatch, on=True)
    ws = "ws_untiered_domain"
    site_id = await _seed_site(workspace_id=ws, plan_tier=None)
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.untiered.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.untiered.com"


async def test_a_refused_attach_never_reaches_cloudflare(monkeypatch):
    """The gate runs BEFORE any Cloudflare call.

    Raising after ``create_custom_hostname`` succeeds would leave a hostname on the
    shared zone with no Site row pointing at it — invisible to the product, and it
    makes the customer's next legitimate attach fail on a 1406 duplicate they
    cannot see or clear.

    Driven through the COUNT gate now, since that is the one a free workspace can
    actually trip: one site already holds a domain, a second one asks.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_no_cf_call"
    first = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    await sites_service.add_domain(
        workspace_id=ws, site_id=first, hostname="www.first.com", _cloudflare=_RecordingCF()
    )
    second = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier(), pocket_id="pk_2")
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=second,
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
    first = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    await sites_service.add_domain(
        workspace_id=ws, site_id=first, hostname="www.taken.com", _cloudflare=_RecordingCF()
    )
    second = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier(), pocket_id="pk_2")
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=second,
            hostname="www.nowrite.com",
            _cloudflare=cf,
        )

    doc = await Site.get(second)
    assert doc.domains == []
    assert "www.nowrite.com" not in doc.allowed_origins


# --------------------------------------------------------------------------- #
# The wider half of the hole: a paid TIER without a paying SUBSCRIPTION.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["none", "pending", "cancelled"])
async def test_a_paid_tier_without_an_active_subscription_falls_to_the_floor(monkeypatch, status):
    """Cancellation never resets ``plan_tier``, and an unconfigured Dodo product
    records a paid tier with no charge at all — so the tier alone still cannot be
    trusted, and the resolver still gates the PAID grants on the subscription.

    What changed is where the site lands when it fails that gate: on the free
    floor, which now includes one domained site. So this attach SUCCEEDS, and the
    thing the lapsed site lost is its uncapped allowance — the next test proves it
    is genuinely capped rather than silently still uncapped.
    """
    _enforce(monkeypatch, on=True)
    ws = f"ws_paid_{status}"
    site_id = await _seed_site(
        workspace_id=ws,
        plan_tier=_a_tier_granting_custom_domain(),
        subscription_status=status,
    )
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname=f"www.{status}.com",
        _cloudflare=cf,
    )

    assert res.hostname == f"www.{status}.com"


async def test_a_lapsed_paid_site_is_capped_like_a_free_one(monkeypatch):
    """The other half: falling to the floor means being SUBJECT to the floor.

    A lapsed ``pro`` site keeps one domained site, not its old uncapped allowance,
    so a second site in the same workspace is refused exactly as it would be under
    free. Without this the previous test would pass just as happily if lapsing did
    nothing at all.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_lapsed_capped"
    lapsed = await _seed_site(
        workspace_id=ws,
        plan_tier=_a_tier_granting_custom_domain(),
        subscription_status="cancelled",
    )
    await sites_service.add_domain(
        workspace_id=ws, site_id=lapsed, hostname="www.lapsed.com", _cloudflare=_RecordingCF()
    )
    other = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier(), pocket_id="pk_2")

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws, site_id=other, hostname="www.other.com", _cloudflare=_RecordingCF()
        )

    assert exc.value.code == "billing.custom_domain_limit"


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
# The capability gate's remaining job.
#
# Free includes a domain, so ``_assert_entitled_to_custom_domain`` fires on no tier
# the catalog currently ships — what bites is the COUNT. That makes the gate easy
# to read as dead code and delete. It is not dead: it is the fail-closed floor for
# a catalog that grants zero, which is exactly what reverting the captain's rule
# would produce. These two prove it still works, so the guard is pinned rather than
# merely present.
# --------------------------------------------------------------------------- #


async def test_a_catalog_granting_no_domain_at_all_still_refuses(monkeypatch):
    """Floor allowance 0 → the capability gate is the whole defence again."""
    _enforce(monkeypatch, on=True)
    monkeypatch.setitem(site_plans._SITE_PLAN_MAX_DOMAINED_SITES, _the_free_tier(), 0)
    ws = "ws_zero_floor"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.zerofloor.com",
            _cloudflare=cf,
        )

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.custom_domain_not_entitled"
    assert cf.create_calls == []


async def test_with_a_zero_floor_a_lapsed_paid_site_is_refused_too(monkeypatch):
    """And the fall-to-the-floor rule falls to whatever the floor actually says.

    The lapsed site is not special-cased anywhere — it simply resolves to the base
    tier's allowance. Set that to 0 and a cancelled subscription refuses again,
    which is the pre-2026-08-21 behaviour, reachable by one catalog edit.
    """
    _enforce(monkeypatch, on=True)
    monkeypatch.setitem(site_plans._SITE_PLAN_MAX_DOMAINED_SITES, _the_free_tier(), 0)
    ws = "ws_zero_floor_lapsed"
    site_id = await _seed_site(
        workspace_id=ws,
        plan_tier=_a_tier_granting_custom_domain(),
        subscription_status="cancelled",
    )
    cf = _RecordingCF()

    with pytest.raises(CloudError) as exc:
        await sites_service.add_domain(
            workspace_id=ws,
            site_id=site_id,
            hostname="www.zerolapsed.com",
            _cloudflare=cf,
        )

    assert exc.value.code == "billing.custom_domain_not_entitled"


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


async def test_with_billing_off_even_a_zero_floor_attaches(monkeypatch):
    """The pair to the zero-floor refusal above, and the only test that can see
    the capability gate's own flag check.

    On the catalog as shipped, dropping ``if not billing_enforced: return`` from
    that gate changes nothing: every tier grants a domain, so the gate returns
    either way and a mutation deleting the check escapes unnoticed. Zero the floor
    and the two paths finally differ — enforced refuses, self-host attaches.
    """
    _enforce(monkeypatch, on=False)
    monkeypatch.setitem(site_plans._SITE_PLAN_MAX_DOMAINED_SITES, _the_free_tier(), 0)
    ws = "ws_oss_zero_floor"
    site_id = await _seed_site(workspace_id=ws, plan_tier=_the_free_tier())
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.ossfloor.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.ossfloor.com"


# --------------------------------------------------------------------------- #
# The gate is only as correct as the fields it reads, and the publish path used
# to rewrite both of them on every republish.
#
# Added 2026-08-15 after review. ``site_plan_key`` is an optional client-supplied
# field, so an ordinary republish that omits it reset ``plan_tier`` to the base key
# and ``subscription_status`` to "none". Nothing restores either — only the
# ``subscription.active`` webhook writes "active", and no path rewrites the tier.
# Harmless while nothing read the fields; this branch makes ``add_domain`` read
# them, which turns the loss into a permanent 402 telling a paying customer to
# upgrade a plan they already bought.
# --------------------------------------------------------------------------- #


async def test_a_republish_without_a_plan_key_keeps_the_paid_tier(monkeypatch):
    """The tier survives a republish that simply does not mention it."""
    from pocketpaw_ee.sites import service as svc

    _enforce(monkeypatch, on=True)
    ws = "ws_keep_tier"
    tier = _a_tier_granting_custom_domain()
    site_id = await _seed_site(workspace_id=ws, plan_tier=tier, subscription_status="active")
    doc = await Site.get(site_id)

    await svc._apply_site_plan(
        doc=doc,
        workspace_id=ws,
        pocket_id="pk_1",
        user_id="u1",
        site_plan_key=None,  # the republish says nothing about the plan
        provider=None,
    )

    refreshed = await Site.get(site_id)
    assert refreshed.plan_tier == tier
    assert refreshed.subscription_status == "active"


async def test_a_republish_leaves_a_paying_site_able_to_attach_a_domain(monkeypatch):
    """The whole point, end to end: republish, then attach. Before the fix this
    402'd with "upgrade this site's plan" at a customer who was still paying."""
    from pocketpaw_ee.sites import service as svc

    _enforce(monkeypatch, on=True)
    ws = "ws_republish_domain"
    tier = _a_tier_granting_custom_domain()
    site_id = await _seed_site(workspace_id=ws, plan_tier=tier, subscription_status="active")
    doc = await Site.get(site_id)
    await svc._apply_site_plan(
        doc=doc,
        workspace_id=ws,
        pocket_id="pk_1",
        user_id="u1",
        site_plan_key=None,
        provider=None,
    )
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname="www.stillpaying.com",
        _cloudflare=cf,
    )

    assert res.hostname == "www.stillpaying.com"


async def test_an_explicit_tier_still_wins(monkeypatch):
    """The fix must not make downgrades impossible. A real plan change carries the
    target tier; only the ABSENCE of one is read as "leave it alone"."""
    from pocketpaw_ee.sites import service as svc

    _enforce(monkeypatch, on=True)
    ws = "ws_explicit_downgrade"
    site_id = await _seed_site(
        workspace_id=ws,
        plan_tier=_a_tier_granting_custom_domain(),
        subscription_status="active",
    )
    doc = await Site.get(site_id)

    await svc._apply_site_plan(
        doc=doc,
        workspace_id=ws,
        pocket_id="pk_1",
        user_id="u1",
        site_plan_key=_the_free_tier(),  # an explicit move to the floor
        provider=None,
    )

    refreshed = await Site.get(site_id)
    assert refreshed.plan_tier == _the_free_tier()
