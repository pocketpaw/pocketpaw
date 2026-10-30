# tests/cloud/sites/test_custom_domain_cap.py — free includes a custom domain on
# EVERY site, and this tree is the per-site count that bounds it.
#
# Pricing is per site, so the free allowance is per site: each free site may
# carry one custom domain, apex + ``www``, and nothing a sibling site holds counts
# against it. Its sibling tree, test_custom_domain_entitlement.py, covers the
# CAPABILITY gate (may this site have a domain at all). This one covers the COUNT
# (has THIS site used its free hostnames). Different question, different 402 code,
# different remedy, so they are deliberately not one file.
#
# What is pinned here:
#   * apex + ``www`` both land on one free site, and a third hostname is refused
#   * every free site in a workspace gets its own domain
#   * the refusal comes before any Cloudflare call and leaves no half-state
#   * paid sites are uncapped, and archived or foreign sites change nothing
#   * re-adding a connected hostname is never refused, even on a full site
#   * billing off means no cap at all, and the gate never scans sibling sites
#
# Every tier key is read off the catalog, never written as a literal, so a
# pricing rekey moves this tree with the ladder instead of breaking it.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

pytestmark = pytest.mark.usefixtures("mongo_db")


def _the_free_tier() -> str:
    return site_plans.BASE_SITE_PLAN_KEY


def _an_uncapped_tier() -> str:
    """The cheapest catalog tier with no ceiling on custom domains."""
    for tier in site_plans.list_site_plans():
        if tier.max_domained_sites is None:
            return tier.key
    raise AssertionError("no site plan tier has an uncapped domain allowance — catalog changed")


class _RecordingCF:
    """Stand-in CloudflareClient that records calls and never touches the network."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.route_calls: list[dict] = []
        self.delete_calls: list[str] = []

    async def create_custom_hostname(
        self, hostname: str, *, features: set[str] | None = None
    ) -> CustomHostname:
        self.create_calls.append({"hostname": hostname, "features": features})
        return CustomHostname(
            id=f"ch_{len(self.create_calls)}",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def create_worker_route(self, *, pattern: str, script: str) -> str:
        self.route_calls.append({"pattern": pattern, "script": script})
        return f"route_{len(self.route_calls)}"

    async def delete_custom_hostname(self, hostname_id: str) -> None:
        self.delete_calls.append(hostname_id)


def _enforce(monkeypatch, *, on: bool) -> None:
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_site_products=None),
    )


async def _seed_site(
    *,
    workspace_id: str,
    pocket_id: str,
    plan_tier: str | None = None,
    subscription_status: str = "none",
    archived: bool = False,
) -> str:
    """Insert a real Site doc and return its id.

    ``deployed=True`` throughout: an unpublished site is refused by a DIFFERENT
    guard (``sites.domain_needs_publish``), and a test that tripped that one while
    believing it proved the cap would pass for the wrong reason.
    """
    doc = Site(
        workspace=workspace_id,
        pocket_id=pocket_id,
        owner="u1",
        name=f"Site {pocket_id}",
        plan_tier=plan_tier,
        subscription_status=subscription_status,
        deployed=True,
        archived=archived,
    )
    await doc.insert()
    return str(doc.id)


async def _attach(ws: str, site_id: str, hostname: str, cf: _RecordingCF | None = None):
    return await sites_service.add_domain(
        workspace_id=ws,
        site_id=site_id,
        hostname=hostname,
        _cloudflare=cf or _RecordingCF(),
    )


async def _fill(ws: str, site_id: str, domain: str) -> None:
    """Spend a free site's whole hostname allowance: apex, then ``www``."""
    hosts = [domain, f"www.{domain}"]
    for host in hosts[: site_plans.free_max_hostnames_per_site()]:
        await _attach(ws, site_id, host)


# --------------------------------------------------------------------------- #
# One free domain is apex + www on the same site.
# --------------------------------------------------------------------------- #


async def test_apex_and_www_both_land_on_one_free_site(monkeypatch):
    """The pair every customer wants. ``acme.com`` and ``www.acme.com`` are two
    ``SiteDomain`` rows on ONE site, and both must succeed on the free floor."""
    _enforce(monkeypatch, on=True)
    ws = "ws_apex_www"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())

    await _attach(ws, site_id, "acme.com")
    await _attach(ws, site_id, "www.acme.com")

    doc = await Site.get(site_id)
    assert [d.hostname for d in doc.domains] == ["acme.com", "www.acme.com"]


# --------------------------------------------------------------------------- #
# Per site, not per workspace — a sibling's domain never closes this site's slot.
# --------------------------------------------------------------------------- #


async def test_every_free_site_gets_its_own_domain(monkeypatch):
    """Three free sites in one workspace, each with its own apex + www."""
    _enforce(monkeypatch, on=True)
    ws = "ws_every_site"
    for i in range(3):
        site_id = await _seed_site(workspace_id=ws, pocket_id=f"pk_{i}", plan_tier=_the_free_tier())
        await _fill(ws, site_id, f"site{i}.com")

        doc = await Site.get(site_id)
        assert len(doc.domains) == site_plans.free_max_hostnames_per_site()


async def test_the_refusal_precedes_every_cloudflare_call(monkeypatch):
    """A refusal after ``create_custom_hostname`` strands a hostname on the shared
    zone: invisible to the product, and it makes the customer's next legitimate
    attach fail on a 1406 duplicate they can neither see nor clear."""
    _enforce(monkeypatch, on=True)
    ws = "ws_cap_no_cf"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _fill(ws, site_id, "first.com")
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await _attach(ws, site_id, "extra.first.com", cf)

    assert cf.create_calls == []
    assert cf.route_calls == []


async def test_a_refused_attach_keeps_a_clean_document(monkeypatch):
    """No half-state on the refused site: the domains and ``allowed_origins`` it
    had before, and nothing more. ``allowed_origins`` is what authorizes a host to
    POST captures at the site, so a stray entry there is a real capture hole."""
    _enforce(monkeypatch, on=True)
    ws = "ws_cap_clean"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _fill(ws, site_id, "first.com")
    before = await Site.get(site_id)

    with pytest.raises(CloudError):
        await _attach(ws, site_id, "extra.first.com")

    doc = await Site.get(site_id)
    assert [d.hostname for d in doc.domains] == [d.hostname for d in before.domains]
    assert doc.allowed_origins == before.allowed_origins
    assert "extra.first.com" not in doc.allowed_origins


# --------------------------------------------------------------------------- #
# Paid sites and siblings.
# --------------------------------------------------------------------------- #


async def test_a_paid_sites_domain_does_not_consume_the_free_allowance(monkeypatch):
    """The mixed workspace: a paying site holds a domain, and a free site beside it
    still attaches its own. Buying a plan for one site must never take the free
    domain away from another."""
    _enforce(monkeypatch, on=True)
    ws = "ws_mixed"
    paid = await _seed_site(
        workspace_id=ws,
        pocket_id="pk_paid",
        plan_tier=_an_uncapped_tier(),
        subscription_status="active",
    )
    await _attach(ws, paid, "www.paid.com")
    free_site = await _seed_site(workspace_id=ws, pocket_id="pk_free", plan_tier=_the_free_tier())

    res = await _attach(ws, free_site, "www.free.com")

    assert res.hostname == "www.free.com"


async def test_an_uncapped_site_is_never_refused_however_many_siblings_have_domains(monkeypatch):
    """The paid tier's actual product: no ceiling. Free-floor sites elsewhere in
    the workspace do not stand between a paying site and its domain."""
    _enforce(monkeypatch, on=True)
    ws = "ws_uncapped"
    other = await _seed_site(workspace_id=ws, pocket_id="pk_other", plan_tier=_the_free_tier())
    await _attach(ws, other, "www.other.com")
    paid = await _seed_site(
        workspace_id=ws,
        pocket_id="pk_paid",
        plan_tier=_an_uncapped_tier(),
        subscription_status="active",
    )

    res = await _attach(ws, paid, "www.paid.com")

    assert res.hostname == "www.paid.com"


async def test_another_workspaces_domains_are_invisible(monkeypatch):
    """Tenancy. Nothing another workspace attaches has any bearing here."""
    _enforce(monkeypatch, on=True)
    neighbour = await _seed_site(
        workspace_id="ws_neighbour", pocket_id="pk_1", plan_tier=_the_free_tier()
    )
    await _attach("ws_neighbour", neighbour, "www.neighbour.com")
    mine = await _seed_site(workspace_id="ws_mine", pocket_id="pk_1", plan_tier=_the_free_tier())

    res = await _attach("ws_mine", mine, "www.mine.com")

    assert res.hostname == "www.mine.com"


async def test_an_archived_duplicate_does_not_spend_the_allowance(monkeypatch):
    """Archived sites are dedupe tombstones (PERF-2), not sites a user can see. A
    tombstone holding a domain must not stand in the way of the live site's own."""
    _enforce(monkeypatch, on=True)
    ws = "ws_archived"
    ghost = await _seed_site(
        workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier(), archived=True
    )
    # Attach while it is still visible, then tombstone it — the order a dedupe run
    # actually produces.
    doc = await Site.get(ghost)
    doc.archived = False
    await doc.save()
    await _attach(ws, ghost, "www.ghost.com")
    doc = await Site.get(ghost)
    doc.archived = True
    await doc.save()

    live = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())
    res = await _attach(ws, live, "www.live.com")

    assert res.hostname == "www.live.com"


# --------------------------------------------------------------------------- #
# Never retroactive — the posture the capability gate already had, re-pinned for
# the count because the count is the easier one to place wrongly.
# --------------------------------------------------------------------------- #


async def test_re_adding_a_connected_hostname_is_never_refused_for_quota(monkeypatch):
    """Pressing Add on a hostname the site already has must stay a no-op that
    returns the stored row, even when the site is already at its hostname cap.

    It is the only self-service repair for a domain connected before the routing
    lane shipped — those have no Worker route and silently serve the fallback
    origin while Cloudflare reports them active. Placing the count gate above the
    already-connected branch would make a full site unable to fix them.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_repair_over_cap"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _fill(ws, site_id, "legacy.com")
    # Strip the route and stamp a deploy target, reproducing a pre-routing domain.
    doc = await Site.get(site_id)
    doc.deploy_target = "workers"
    doc.domains[-1].cf_route_id = ""
    await doc.save()
    hostname = doc.domains[-1].hostname
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws, site_id=site_id, hostname=hostname, _cloudflare=cf
    )

    assert res.hostname == hostname
    assert len(cf.route_calls) == 1
    assert cf.create_calls == []


# --------------------------------------------------------------------------- #
# The per-site hostname cap — one constant, one comparison.
# --------------------------------------------------------------------------- #


async def test_a_free_site_may_carry_apex_plus_www_and_no_more(monkeypatch):
    """Without a ceiling a free site could point fifty domains at itself, each
    costing a Cloudflare custom hostname and a Worker route at $0 revenue. Two is
    apex + www."""
    _enforce(monkeypatch, on=True)
    ws = "ws_hostname_guard"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    for i in range(site_plans.free_max_hostnames_per_site()):
        await _attach(ws, site_id, f"host{i}.acme.com")

    with pytest.raises(CloudError) as exc:
        await _attach(ws, site_id, "onemore.acme.com")

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.custom_domain_limit"
    assert str(site_plans.free_max_hostnames_per_site()) in exc.value.message


async def test_the_hostname_guard_does_not_apply_to_a_paying_site(monkeypatch):
    """A paid site's product is an uncapped allowance, and that includes hostnames."""
    _enforce(monkeypatch, on=True)
    ws = "ws_hostname_paid"
    site_id = await _seed_site(
        workspace_id=ws,
        pocket_id="pk_1",
        plan_tier=_an_uncapped_tier(),
        subscription_status="active",
    )
    for i in range(site_plans.free_max_hostnames_per_site() + 2):
        await _attach(ws, site_id, f"host{i}.acme.com")

    doc = await Site.get(site_id)
    assert len(doc.domains) == site_plans.free_max_hostnames_per_site() + 2


# --------------------------------------------------------------------------- #
# OSS / self-host — billing off means no paywall.
# --------------------------------------------------------------------------- #


async def test_with_billing_off_a_free_site_is_not_capped(monkeypatch):
    """Self-host has no billing and must not inherit a cap from this branch."""
    _enforce(monkeypatch, on=False)
    ws = "ws_oss_cap"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    for i in range(site_plans.free_max_hostnames_per_site() + 1):
        await _attach(ws, site_id, f"host{i}.acme.com")

    doc = await Site.get(site_id)
    assert len(doc.domains) == site_plans.free_max_hostnames_per_site() + 1


async def test_the_attach_never_scans_sibling_sites(monkeypatch):
    """The allowance is per site, so the gate reads only the site it loaded.

    ``_load`` uses ``find_one``; a ``find`` on this path would mean a workspace
    census has crept back in. Checked with billing ON, where it would bite.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_no_census"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())

    finds: list[object] = []
    original = sites_service._SiteDoc.find

    def _spy(*args, **kwargs):
        finds.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(sites_service._SiteDoc, "find", _spy)

    await _attach(ws, site_id, "www.noread.com")

    assert finds == []
