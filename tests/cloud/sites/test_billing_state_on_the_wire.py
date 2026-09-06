# tests/cloud/sites/test_billing_state_on_the_wire.py — proves the API actually
# SENDS the per-site billing state the UI is written against.
#
# The defect (found 2026-08-22, feat/site-entitlement-ui-state): the frontend's
# SiteSummary and SiteStatusResponse both declare ``plan_tier``,
# ``subscription_status`` and ``renewal_date``, and the [siteId] page
# branches on them — an "awaiting checkout" bar keys on
# ``subscription_status === "pending"``, and the Billing tab renders the current
# plan. Neither backend DTO carried a single one of those fields:
#
#     SiteResponse        20 fields, billing fields: NONE
#     SiteStatusResponse  13 fields, billing fields: NONE
#
# So every read yielded ``undefined``, every fallback took the "no per-site sub"
# branch, and a paid site was indistinguishable from a free one in the UI. The
# optional ``?`` on the TypeScript fields is what kept it quiet: nothing throws
# when a field that is always absent is always absent.
#
# It survived because the frontend test does not test the page. It reimplements
# the page's derivation as a local ``isPendingPayment`` helper and feeds it
# hand-written strings — so it proves the rule is right while saying nothing about
# whether the inputs ever arrive.
#
# These tests assert on the DTO the router actually returns, which is the only
# place that question can be answered.
#
# Created 2026-08-22 (feat/site-entitlement-ui-state): new test module.
#
# Updated 2026-09-02 (feat/sites-analytics-entitlement-field, SA-5): covers
# ``SiteEntitlementsResponse.analytics``. The headline case is the same shape as the
# original defect above — a capability the UI branches on that the wire never sent —
# so it is asserted on the DTO the router returns rather than on the resolver. The
# load-bearing test is the agreement one: three seams now answer "may this site's
# visitors be counted", and it asserts the wire EQUALS the shared predicate rather
# than restating the expected booleans, so a second copy of the rule fails here even
# while it still happens to agree with the catalog.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from pocketpaw_ee.cloud.models.site import Site, SiteDomain
from pocketpaw_ee.sites import service as sites_service

from pocketpaw import config as ppconfig


def _enforce(monkeypatch, *, on: bool = True) -> None:
    """Sites billing enforcement, same seam the cap tests use. The entitlement
    ANSWER (what the plan grants) is independent of it; the domain SLOT answer is
    not — with enforcement off nothing is capped, which is the correct thing for
    the UI to report too."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_site_products=None),
    )


async def _make_workspace() -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    # ``uuid4`` and not a timestamp. A slug built from ``datetime.now()`` collides
    # when two workspaces are created in the same clock tick, which on Windows is
    # ~15.6 ms — comfortably longer than two inserts. The tenancy test below makes
    # exactly that call twice in a row, and a collision hands it the SAME workspace
    # for owner and intruder, so the read it expects to be refused succeeds and the
    # test fails intermittently for a reason that has nothing to do with tenancy.
    ws = Workspace(
        name="Acme",
        slug=f"acme-wire-{uuid4().hex}",
        owner="u1",
        plan="pro",
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(*, workspace_id: str) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name="My Landing",
        owner="u1",
        type="site",
        pattern="landing",
    )
    await doc.insert()
    return str(doc.id)


async def _seed_site(
    *,
    workspace_id: str,
    pocket_id: str,
    plan_tier: str | None = "pro",
    subscription_status: str = "active",
    renewal: datetime | None = None,
    domains: list[str] | None = None,
) -> Site:
    doc = Site(
        workspace=workspace_id,
        pocket_id=pocket_id,
        name="My Landing",
        script_name="site-wire",
        owner="u1",
        deployed=True,
        signed_key="site_key_wire",
        plan_tier=plan_tier,
        subscription_status=subscription_status,
        renewal_date=renewal,
        domains=[SiteDomain(hostname=h) for h in (domains or [])],
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# The plan state the UI already branches on
# ---------------------------------------------------------------------------


async def test_the_site_response_carries_the_plan_the_ui_renders(mongo_db):
    """``plan_tier`` / ``subscription_status`` are on the document already, so
    sending them costs no extra query — they were simply never mapped."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    renewal = datetime(2027, 1, 15, tzinfo=UTC)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, renewal=renewal, subscription_status="active"
    )

    resp = sites_service._to_response(doc)

    assert resp.plan_tier == "site"
    assert resp.subscription_status == "active"
    assert resp.renewal_date is not None
    assert resp.renewal_date.startswith("2027-01-15")


async def test_a_pending_site_says_pending_rather_than_nothing(mongo_db):
    """The [siteId] page's "awaiting checkout" bar keys on exactly this string.
    While the field was absent it read ``undefined`` and the bar never showed, so
    a buyer who abandoned a checkout saw a site that looked simply broken."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(workspace_id=ws, pocket_id=pocket_id, subscription_status="pending")

    assert sites_service._to_response(doc).subscription_status == "pending"


async def test_a_site_with_no_per_site_sub_reports_the_floor_not_null(mongo_db):
    """A free site must be positively described, not described by absence. "none"
    and "the backend forgot to send it" have to be distinguishable on the wire."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier=None, subscription_status="none"
    )

    resp = sites_service._to_response(doc)

    assert resp.subscription_status == "none"
    assert resp.plan_tier in ("", None)


# ---------------------------------------------------------------------------
# The resolved entitlements — what the UI needs to disable a control and say why
# ---------------------------------------------------------------------------


async def test_entitlements_say_whether_this_site_may_take_a_domain(mongo_db, monkeypatch):
    """The domain form must be able to ask before it offers the button."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(workspace_id=ws, pocket_id=pocket_id)

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.custom_domain is True
    assert ent.subscription_active is True
    assert ent.plan_tier == "site"


async def test_a_free_site_reports_its_floor_allowance_not_a_flat_no(mongo_db, monkeypatch):
    """Free includes a custom domain on ONE site. "you cannot" and "you already
    used your one" are different sentences, and the UI can only tell them apart if
    the allowance and the usage both come back."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="free", subscription_status="none"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.custom_domain is True, "the free floor includes one domained site"
    assert ent.max_domained_sites == 1
    assert ent.domained_sites_used == 0
    assert ent.domain_slots_available is True


async def test_the_free_allowance_reads_as_spent_once_another_site_holds_a_domain(
    mongo_db, monkeypatch
):
    """The cap counts SITES, not hostnames, and it excludes the site being asked
    about. Without the usage count the UI cannot grey the button before the 402."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    other_pocket = await _make_pocket(workspace_id=ws)
    this_pocket = await _make_pocket(workspace_id=ws)

    await _seed_site(
        workspace_id=ws,
        pocket_id=other_pocket,
        plan_tier="free",
        subscription_status="none",
        domains=["already.example.com"],
    )
    doc = await _seed_site(
        workspace_id=ws, pocket_id=this_pocket, plan_tier="free", subscription_status="none"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.domained_sites_used == 1
    assert ent.domain_slots_available is False, (
        "the workspace's one free domained site is spent — the UI must say so "
        "instead of offering a button that 402s"
    )


async def test_a_paid_site_is_not_capped_by_the_free_allowance(mongo_db, monkeypatch):
    """An uncapped tier reports None, and slots stay available no matter how many
    other sites hold domains."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    other_pocket = await _make_pocket(workspace_id=ws)
    this_pocket = await _make_pocket(workspace_id=ws)

    await _seed_site(
        workspace_id=ws,
        pocket_id=other_pocket,
        plan_tier="free",
        subscription_status="none",
        domains=["already.example.com"],
    )
    doc = await _seed_site(
        workspace_id=ws, pocket_id=this_pocket, plan_tier="site", subscription_status="active"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.max_domained_sites is None
    assert ent.domain_slots_available is True


async def test_a_lapsed_paid_site_loses_the_capability_and_says_so(mongo_db, monkeypatch):
    """A cancelled subscription falls back to the floor. The UI needs the reason
    to differ from "you never had this" — the site had it and stopped paying."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="site", subscription_status="cancelled"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.subscription_active is False
    assert ent.concierge_entitled is False
    assert ent.plan_tier == "site", "the tier is still recorded; only the payment stopped"


async def test_another_workspace_cannot_read_this_site_s_entitlements(mongo_db, monkeypatch):
    """Entitlements describe what someone is paying for. The read is tenant-scoped
    through ``_load`` like every other site read; a raw ``find_one`` on the id
    alone would answer for any workspace's site to any caller."""
    import pytest
    from pocketpaw_ee.cloud._core.errors import NotFound

    _enforce(monkeypatch)
    owner_ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=owner_ws)
    doc = await _seed_site(workspace_id=owner_ws, pocket_id=pocket_id)

    intruder_ws = await _make_workspace()

    with pytest.raises(NotFound):
        await sites_service.site_entitlements(workspace_id=intruder_ws, site_id=str(doc.id))


async def test_no_capability_means_no_slot_however_empty_the_workspace(mongo_db, monkeypatch):
    """A tier that cannot hold a domain at all must not be offered a slot.

    No tier in today's catalog can reach this: every one of them inherits the free
    floor's single domained site, so ``custom_domain`` is True everywhere and the
    guard is unreachable by construction. It is still the correct rule — the cap
    answers "how many more", not "may you at all", and a tier mapped to 0 would
    otherwise be handed a slot and then refused with
    ``billing.custom_domain_not_entitled``.

    Rather than leave that as dead code asserting nothing, this drives the resolver
    to the state the catalog cannot currently produce, so the guard is observed
    rather than assumed. If a paid-only-domains tier ever ships, this is already
    its test.
    """
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service
    from pocketpaw_ee.cloud.entitlements.domain import SiteEntitlements

    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="free", subscription_status="none"
    )

    monkeypatch.setattr(
        entitlements_service,
        "resolve_site_entitlements",
        lambda **kw: SiteEntitlements(
            site_id=kw["site_id"],
            workspace_id=kw["workspace_id"],
            plan_tier="free",
            subscription_active=False,
            badge_required=True,
            custom_domain=False,
            max_domained_sites=0,
            analytics=False,
            concierge_enabled=False,
            concierge_entitled=False,
        ),
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.custom_domain is False
    assert ent.domain_slots_available is False, (
        "an empty workspace made a slot look available on a tier that cannot hold "
        "a domain — the button would enable and then 402"
    )


# ---------------------------------------------------------------------------
# The analytics grant (SA-5) — the pre-check that keeps the panel from
# discovering a refusal by rendering it
# ---------------------------------------------------------------------------


async def test_a_paid_site_reports_that_its_visitors_may_be_counted(mongo_db, monkeypatch):
    """The whole point of the field: the panel can enable itself without calling
    the analytics endpoint first."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="site", subscription_status="active"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.analytics is True


async def test_a_free_site_reports_no_analytics(mongo_db, monkeypatch):
    """A free site's traffic is never counted — a Worker invocation is billed and
    a static asset is not, so the grant has to track the plan."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="free", subscription_status="none"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.analytics is False


async def test_a_lapsed_paid_site_loses_analytics_though_it_keeps_the_tier(mongo_db, monkeypatch):
    """The bug this whole module exists to prevent, on the newest field. Cancelling
    leaves ``plan_tier`` on the paid key — nothing resets it — so a field that read
    the tier alone would grant analytics to a cancelled site forever."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="site", subscription_status="cancelled"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.plan_tier == "site", "the tier is still recorded; only the payment stopped"
    assert ent.analytics is False


async def test_the_wire_field_agrees_with_the_predicate_the_publish_seam_reads(
    mongo_db, monkeypatch
):
    """The anti-drift assertion, and the reason this field calls
    ``site_analytics_entitled`` instead of re-deriving the rule.

    Three seams now answer "may this site's visitors be counted": the publish path
    (which decides whether the deployed config carries a counter at all), the read
    endpoint (which decides whether numbers may be served), and this field (which
    decides what the dashboard offers). They must never disagree. A site whose
    publish counted but whose panel is greyed out is a customer paying for a chart
    they cannot open; the reverse offers a chart that 402s.

    So this drives the endpoint across the states that separate them and asserts the
    wire value EQUALS the predicate, rather than restating the expected booleans. A
    future edit that reintroduces a second copy of the rule fails here even if it
    happens to agree with the catalog today.
    """
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    _enforce(monkeypatch)
    ws = await _make_workspace()

    cases = [
        ("site", "active"),
        ("staff", "active"),
        ("free", "none"),
        ("free", "active"),
        ("site", "cancelled"),
        ("site", "pending"),
        (None, "active"),
        ("not-a-tier", "active"),
    ]

    for plan_tier, subscription_status in cases:
        pocket_id = await _make_pocket(workspace_id=ws)
        doc = await _seed_site(
            workspace_id=ws,
            pocket_id=pocket_id,
            plan_tier=plan_tier,
            subscription_status=subscription_status,
        )

        ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))
        expected = entitlements_service.site_analytics_entitled(
            plan_tier=plan_tier, subscription_status=subscription_status
        )

        assert ent.analytics is expected, (
            f"the wire disagreed with the predicate for {plan_tier!r}/"
            f"{subscription_status!r}: wire={ent.analytics}, predicate={expected}"
        )


async def test_the_cases_are_not_all_the_same_answer(mongo_db, monkeypatch):
    """Guards the test above from decaying into a tautology. If the catalog ever
    stopped granting analytics to any tier, every case would agree at False and the
    agreement test would still pass while proving nothing."""
    from pocketpaw_ee.cloud.entitlements import service as entitlements_service

    assert (
        entitlements_service.site_analytics_entitled(plan_tier="site", subscription_status="active")
        is True
    )
    assert (
        entitlements_service.site_analytics_entitled(plan_tier="free", subscription_status="active")
        is False
    )


def test_the_response_defaults_refuse_rather_than_grant():
    """Every capability on ``SiteEntitlementsResponse`` defaults to the REFUSING
    direction, so a partially-populated response can never hand out a capability by
    omission. This is the only thing standing behind ``analytics``'s default: the
    single construction site sets it explicitly, so a flipped default would ship
    unnoticed until the second caller appeared."""
    from pocketpaw_ee.sites.dto import SiteEntitlementsResponse

    bare = SiteEntitlementsResponse(site_id="s1")

    assert bare.analytics is False
    assert bare.custom_domain is False
    assert bare.concierge_entitled is False
    assert bare.subscription_active is False
    assert bare.badge_required is True, "the badge default is a requirement, not a grant"
