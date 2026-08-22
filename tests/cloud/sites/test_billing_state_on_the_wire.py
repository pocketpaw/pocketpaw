# tests/cloud/sites/test_billing_state_on_the_wire.py — proves the API actually
# SENDS the per-site billing state the UI is written against.
#
# The defect (found 2026-08-22, feat/site-entitlement-ui-state): the frontend's
# SiteSummary and SiteStatusResponse both declare ``plan_tier``,
# ``subscription_status`` and ``annual_renewal_date``, and the [siteId] page
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

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

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

    ws = Workspace(
        name="Acme",
        slug=f"acme-wire-{datetime.now(UTC).timestamp()}",
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
        annual_renewal_date=renewal,
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

    assert resp.plan_tier == "pro"
    assert resp.subscription_status == "active"
    assert resp.annual_renewal_date is not None
    assert resp.annual_renewal_date.startswith("2027-01-15")


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
    assert ent.plan_tier == "pro"


async def test_a_free_site_reports_its_floor_allowance_not_a_flat_no(mongo_db, monkeypatch):
    """Free includes a custom domain on ONE site. "you cannot" and "you already
    used your one" are different sentences, and the UI can only tell them apart if
    the allowance and the usage both come back."""
    _enforce(monkeypatch)
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)
    doc = await _seed_site(
        workspace_id=ws, pocket_id=pocket_id, plan_tier="basic", subscription_status="none"
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
        plan_tier="basic",
        subscription_status="none",
        domains=["already.example.com"],
    )
    doc = await _seed_site(
        workspace_id=ws, pocket_id=this_pocket, plan_tier="basic", subscription_status="none"
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
        plan_tier="basic",
        subscription_status="none",
        domains=["already.example.com"],
    )
    doc = await _seed_site(
        workspace_id=ws, pocket_id=this_pocket, plan_tier="pro", subscription_status="active"
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
        workspace_id=ws, pocket_id=pocket_id, plan_tier="pro", subscription_status="cancelled"
    )

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.subscription_active is False
    assert ent.concierge_entitled is False
    assert ent.plan_tier == "pro", "the tier is still recorded; only the payment stopped"


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
        workspace_id=ws, pocket_id=pocket_id, plan_tier="basic", subscription_status="none"
    )

    monkeypatch.setattr(
        entitlements_service,
        "resolve_site_entitlements",
        lambda **kw: SiteEntitlements(
            site_id=kw["site_id"],
            workspace_id=kw["workspace_id"],
            plan_tier="basic",
            subscription_active=False,
            badge_required=True,
            custom_domain=False,
            max_domained_sites=0,
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
