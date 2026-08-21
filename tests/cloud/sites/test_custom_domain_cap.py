# tests/cloud/sites/test_custom_domain_cap.py — free includes a custom domain on
# ONE site, and this tree is the count that enforces the word "one".
#
# Created 2026-08-21 (feat/site-free-custom-domain, PW-1). The rule: "only 1 site
# is allowed to have a custom domain in free" (captain, 2026-08-21). Its sibling
# tree, test_custom_domain_entitlement.py, covers the CAPABILITY gate — may this
# site have a domain at all. This one covers the COUNT — has the workspace room
# for another site that has one. Different question, different 402 code, different
# remedy, so they are deliberately not one file.
#
# THE UNIT IS THE SITE, NOT THE HOSTNAME, and that is the single thing most worth
# pinning here. ``SiteDomain`` is one row per hostname, so a count that reads rows
# refuses apex + ``www`` — the pair almost every customer wants — while looking
# completely correct. The first test in the file is that pair, on purpose: it is
# the case an earlier draft of this design got wrong.
#
# Three counting rules, one test each, plus the guard that keeps the site-unit rule
# from being free-for-all:
#   1. sites holding >= 1 domain, not hostnames  -> the apex + www test
#   2. FLOOR sites only                          -> the mixed-workspace test
#   3. the target site excluded from its own cap -> the apex + www test again
#   + a per-site hostname cap, since (1) leaves hostname count unbounded
#
# Every tier key is read off the catalog, never written as a literal, so the
# pricing-spec rekey (basic/pro/business -> free/site/staff) moves this tree with
# the ladder instead of breaking it.

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
    """The cheapest catalog tier with no ceiling on domained sites."""
    for tier in site_plans.list_site_plans():
        if tier.max_domained_sites is None:
            return tier.key
    raise AssertionError("no site plan tier has an uncapped domain allowance — catalog changed")


def _the_free_site_limit() -> int:
    """How many domained sites the floor allows, read from the catalog."""
    floor = site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY)
    assert floor is not None and floor.max_domained_sites is not None
    return floor.max_domained_sites


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


# --------------------------------------------------------------------------- #
# Rule 1 + rule 3 — the unit is the SITE, and a site is not counted against
# itself. This is the test the whole design turns on.
# --------------------------------------------------------------------------- #


async def test_apex_and_www_both_land_on_one_free_site(monkeypatch):
    """The pair every customer wants, and the case a hostname-counting cap refuses.

    ``acme.com`` and ``www.acme.com`` are two ``SiteDomain`` rows on ONE site. Under
    a cap of one they must both succeed, because the cap counts sites. If this ever
    fails with ``billing.custom_domain_limit``, the count has drifted back to rows.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_apex_www"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())

    await _attach(ws, site_id, "acme.com")
    await _attach(ws, site_id, "www.acme.com")

    doc = await Site.get(site_id)
    assert [d.hostname for d in doc.domains] == ["acme.com", "www.acme.com"]


# --------------------------------------------------------------------------- #
# The cap itself — the SECOND site is where free stops.
# --------------------------------------------------------------------------- #


async def test_a_second_free_site_is_refused(monkeypatch):
    """One site's worth of custom domain, and the second site asking is told so."""
    _enforce(monkeypatch, on=True)
    ws = "ws_second_site"
    first = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _attach(ws, first, "www.first.com")
    second = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())

    with pytest.raises(CloudError) as exc:
        await _attach(ws, second, "www.second.com")

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.custom_domain_limit"
    assert str(_the_free_site_limit()) in exc.value.message


async def test_the_refusal_precedes_every_cloudflare_call(monkeypatch):
    """A refusal after ``create_custom_hostname`` strands a hostname on the shared
    zone: invisible to the product, and it makes the customer's next legitimate
    attach fail on a 1406 duplicate they can neither see nor clear."""
    _enforce(monkeypatch, on=True)
    ws = "ws_cap_no_cf"
    first = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _attach(ws, first, "www.first.com")
    second = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())
    cf = _RecordingCF()

    with pytest.raises(CloudError):
        await _attach(ws, second, "www.second.com", cf)

    assert cf.create_calls == []
    assert cf.route_calls == []


async def test_a_refused_second_site_keeps_a_clean_document(monkeypatch):
    """No half-state on the refused site: nothing on ``domains``, and nothing on
    ``allowed_origins`` either — that list is what authorizes a host to POST
    captures at the site, so a stray entry there is a real capture hole."""
    _enforce(monkeypatch, on=True)
    ws = "ws_cap_clean"
    first = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _attach(ws, first, "www.first.com")
    second = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())

    with pytest.raises(CloudError):
        await _attach(ws, second, "www.second.com")

    doc = await Site.get(second)
    assert doc.domains == []
    assert doc.allowed_origins == []


# --------------------------------------------------------------------------- #
# Rule 2 — count FLOOR sites only. A paying site does not eat the free allowance.
# --------------------------------------------------------------------------- #


async def test_a_paid_sites_domain_does_not_consume_the_free_allowance(monkeypatch):
    """The mixed workspace, and the rule easiest to leave out.

    Site B pays for an uncapped allowance and holds a domain; site C is on the
    floor and holds none. C's first attach must succeed — its allowance is one and
    nothing on the floor has spent it. A census that counted every domained site
    would refuse C, which means buying a plan for one site would silently take the
    free domain away from every other site in the workspace.
    """
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
    """The paid tier's actual product: no ceiling. Two free-floor sites elsewhere
    in the workspace do not stand between a paying site and its domain."""
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
    """Tenancy. The census is scoped to one workspace, so a busy neighbour cannot
    spend this workspace's allowance."""
    _enforce(monkeypatch, on=True)
    neighbour = await _seed_site(
        workspace_id="ws_neighbour", pocket_id="pk_1", plan_tier=_the_free_tier()
    )
    await _attach("ws_neighbour", neighbour, "www.neighbour.com")
    mine = await _seed_site(workspace_id="ws_mine", pocket_id="pk_1", plan_tier=_the_free_tier())

    res = await _attach("ws_mine", mine, "www.mine.com")

    assert res.hostname == "www.mine.com"


async def test_an_archived_duplicate_does_not_spend_the_allowance(monkeypatch):
    """Archived sites are dedupe tombstones (PERF-2), not sites a user can see.

    Counting one would charge a workspace twice for a single site it only has one
    card for, and the user would have no way to find the row consuming their
    domain. Matches the gallery read's ``archived: {"$ne": True}``.
    """
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
# the count because the count is the newer and easier one to place wrongly.
# --------------------------------------------------------------------------- #


async def test_re_adding_a_connected_hostname_is_never_refused_for_quota(monkeypatch):
    """Pressing Add on a hostname the site already has must stay a no-op that
    returns the stored row, even when the workspace is over its cap.

    It is the only self-service repair for a domain connected before the routing
    lane shipped — those have no Worker route and silently serve the fallback
    origin while Cloudflare reports them active. Placing the count gate above the
    already-connected branch would make a workspace at its cap unable to fix them.
    """
    _enforce(monkeypatch, on=True)
    ws = "ws_repair_over_cap"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _attach(ws, site_id, "www.legacy.com")
    # Strip the route and stamp a deploy target, reproducing a pre-routing domain.
    doc = await Site.get(site_id)
    doc.deploy_target = "workers"
    doc.domains[0].cf_route_id = ""
    await doc.save()
    # ...and put the workspace over its cap from a second site, so a quota check
    # placed above the repair would fire.
    over = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())
    over_doc = await Site.get(over)
    over_doc.domains = (await Site.get(site_id)).domains
    await over_doc.save()
    cf = _RecordingCF()

    res = await sites_service.add_domain(
        workspace_id=ws, site_id=site_id, hostname="www.legacy.com", _cloudflare=cf
    )

    assert res.hostname == "www.legacy.com"
    assert len(cf.route_calls) == 1
    assert cf.create_calls == []


# --------------------------------------------------------------------------- #
# The per-site hostname guard. A recommendation this build made, not a rule the
# captain handed down — one constant, one comparison, deletable in a line.
# --------------------------------------------------------------------------- #


async def test_a_free_site_may_carry_apex_plus_www_and_no_more(monkeypatch):
    """The site-unit cap leaves hostname count unbounded, so a free workspace could
    otherwise point fifty domains at its one allowed site, each costing a Cloudflare
    custom hostname and a Worker route at $0 revenue. Two is apex + www."""
    _enforce(monkeypatch, on=True)
    ws = "ws_hostname_guard"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    for i in range(site_plans.free_max_hostnames_per_site()):
        await _attach(ws, site_id, f"host{i}.acme.com")

    with pytest.raises(CloudError) as exc:
        await _attach(ws, site_id, "onemore.acme.com")

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.custom_domain_limit"


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
# OSS / self-host — billing off means no paywall AND no extra read.
# --------------------------------------------------------------------------- #


async def test_with_billing_off_a_second_site_attaches_freely(monkeypatch):
    """Self-host has no billing and must not inherit a cap from this branch."""
    _enforce(monkeypatch, on=False)
    ws = "ws_oss_cap"
    first = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())
    await _attach(ws, first, "www.first.com")
    second = await _seed_site(workspace_id=ws, pocket_id="pk_2", plan_tier=_the_free_tier())

    res = await _attach(ws, second, "www.second.com")

    assert res.hostname == "www.second.com"


async def test_with_billing_off_the_census_query_never_runs(monkeypatch):
    """Not just "no error raised" — no DB read either.

    The census scans every domained site in the workspace. On a self-host install
    that read buys nothing and would still cost a round trip on every attach, so
    the flag check comes first. ``_load`` uses ``find_one``; the census is the only
    caller of ``find`` on this path, which is what makes the assertion specific.
    """
    _enforce(monkeypatch, on=False)
    ws = "ws_oss_noread"
    site_id = await _seed_site(workspace_id=ws, pocket_id="pk_1", plan_tier=_the_free_tier())

    finds: list[object] = []
    original = sites_service._SiteDoc.find

    def _spy(*args, **kwargs):
        finds.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(sites_service._SiteDoc, "find", _spy)

    await _attach(ws, site_id, "www.noread.com")

    assert finds == []
