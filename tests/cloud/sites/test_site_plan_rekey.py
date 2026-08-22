# tests/cloud/sites/test_site_plan_rekey.py — the 2026-08-22 tier rename, driven
# through the real publish path rather than asserted against the catalog alone.
#
# WHY A SEPARATE FILE FROM test_site_pricing_ladder.py. That one is a unit test of
# the catalog: given a key, what does the row say. This one is about what happens
# to a SITE — and the two failure modes it covers are both invisible to a catalog
# test, because both need a document and a publish:
#
#   1. A site published before the rename holds ``plan_tier="pro"``. Everything
#      that reads it must still resolve it, and everything that reports it must
#      report the CURRENT name — otherwise the entitlements endpoint says "site",
#      the site response says "pro", and the plan card matches neither.
#
#   2. An ORG flat (studio/agency) must never become a site's own tier. It is a
#      real, resolvable catalog row that sells badge removal and white-label
#      across a whole workspace; stamped on one site it would hand that site the
#      lot. The publish path has to refuse it, and refusing has to be visible in
#      what gets persisted, not only in a log line.
#
# Created 2026-08-22 (feat/site-pricing-ladder): new test module.

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import service as sites_service


class _RecordingGenerator:
    def __init__(self):
        self.build_calls: list[dict] = []

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.build_calls.append(dict(kw))
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _RecordingCF:
    def __init__(self):
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


async def _make_workspace(plan: str = "pro") -> str:
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(
        name="Acme", slug=f"acme-rekey-{datetime.now(UTC).timestamp()}", owner="u1", plan=plan
    )
    await ws.insert()
    return str(ws.id)


async def _make_pocket(*, workspace_id: str) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id, name="My Landing", owner="u1", type="site", pattern="landing"
    )
    await doc.insert()
    return str(doc.id)


async def _publish(ws: str, pocket_id: str, key: str | None):
    return await sites_service.publish_pocket(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        site_plan_key=key,
        _generator=_RecordingGenerator(),
        _cloudflare=_RecordingCF(),
        _bundle_reader=lambda d: b"x",
    )


# ---------------------------------------------------------------------------
# 1 — a document written before the rename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "current"), [("basic", "free"), ("pro", "site"), ("business", "staff")]
)
async def test_a_pre_rename_document_reports_its_current_tier_on_the_wire(
    mongo_db, legacy, current
):
    """The two reads a client makes about one site must agree.

    ``_to_response`` used to echo ``Site.plan_tier`` verbatim while
    ``resolve_site_entitlements`` resolved it through the catalog. After the
    rename that is two different strings for the same site: the response says
    "pro", entitlements say "site", and the plan card — which matches the
    response's value against the catalog keys from ``GET /billing/site-plans`` —
    matches nothing at all and shows a paying customer no current plan.

    Breaks on: ``_to_response`` going back to ``doc.plan_tier or ""``.
    """
    ws = await _make_workspace()
    doc = Site(
        workspace=ws,
        pocket_id="p1",
        name="Legacy",
        owner="u1",
        plan_tier=legacy,
        subscription_status="active",
    )
    await doc.insert()

    resp = sites_service._to_response(doc)
    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert resp.plan_tier == current
    assert ent.plan_tier == current, "the two reads disagree about one site's plan"


async def test_a_tier_string_nobody_recognises_is_reported_as_found(mongo_db):
    """The mapper echoes an unknown value rather than substituting the floor.

    Both are "wrong" in the sense that the client cannot match either against the
    catalog — but they are wrong differently. Reporting ``free`` is a CONFIDENT
    wrong answer: support reads a plan card saying Free, believes the site is on
    the free tier, and stops looking. Echoing the odd string tells whoever gets
    the ticket what is actually on the document.

    The two entitlement fields still fail closed to the floor; only the reported
    NAME passes through. Asserted together here, because it is the combination
    that is the design — a tier name the resolver refuses to honour.

    Breaks on: ``_canonical_plan_tier`` falling back to ``BASE_SITE_PLAN_KEY``.
    """
    ws = await _make_workspace()
    doc = Site(
        workspace=ws,
        pocket_id="p1",
        name="Odd",
        owner="u1",
        plan_tier="tier-from-the-future",
        subscription_status="active",
    )
    await doc.insert()

    resp = sites_service._to_response(doc)
    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert resp.plan_tier == "tier-from-the-future"
    assert ent.plan_tier == site_plans.BASE_SITE_PLAN_KEY
    assert ent.badge_required is True


async def test_a_pre_rename_paying_site_keeps_its_paid_capabilities(mongo_db):
    """The rename's worst case, stated plainly: every already-published paying
    site holds ``plan_tier="pro"``, and an unrecognised key resolves to the free
    floor. Without the aliases this read returns the badge and revokes the custom
    domain for every paying customer at deploy time, with nothing raised.

    Breaks on: dropping ``pro`` from ``_LEGACY_SITE_TIER_ALIASES``.
    """
    ws = await _make_workspace()
    doc = Site(
        workspace=ws,
        pocket_id="p1",
        name="Paying",
        owner="u1",
        plan_tier="pro",
        subscription_status="active",
    )
    await doc.insert()

    ent = await sites_service.site_entitlements(workspace_id=ws, site_id=str(doc.id))

    assert ent.badge_required is False, "a paying site got its badge back"
    assert ent.custom_domain is True
    assert ent.max_domained_sites is None, "the uncapped allowance was revoked"


async def test_a_republish_that_omits_the_tier_leaves_a_legacy_key_resolvable(mongo_db):
    """A republish with no ``site_plan_key`` keeps the site's existing tier. That
    path reads the stored value back through the catalog, so a legacy key has to
    survive the round trip rather than being treated as unknown and reset to the
    floor."""
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await _publish(ws, pocket_id, None)
    persisted = await Site.find_one(Site.id == doc.id)
    persisted.plan_tier = "pro"
    persisted.subscription_status = "active"
    await persisted.save()

    await _publish(ws, pocket_id, None)

    after = await Site.find_one(Site.id == doc.id)
    assert site_plans.site_scoped_tier(after.plan_tier) is not None
    assert sites_service._to_response(after).plan_tier == "site"


# ---------------------------------------------------------------------------
# 2 — an org flat must not become a site's own tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("org_key", ["studio", "agency"])
async def test_publishing_a_site_on_an_org_flat_records_the_floor_instead(
    mongo_db, org_key, monkeypatch, caplog
):
    """A publish asking for ``studio`` must not stamp ``studio``.

    The Dodo product is configured for EVERY key here on purpose. Without it the
    test would pass through the pre-existing "paid tier with no product" fallback
    and prove nothing about the scope check — the org tier would be refused for
    being unconfigured rather than for being org-scoped, and the day someone
    added a studio product the refusal would silently stop happening.

    THE LOG ASSERTION IS LOAD-BEARING, and this is why. Two guards stand between
    an org key and the document: the scope check, and ``purchasable`` (hard-False
    for org tiers). Belt and braces is right for the code and useless for a test
    — deleting either one leaves the persisted result identical, so a
    stored-state-only assertion measures "at least one guard survives", not the
    one it names. It passed against a mutation that stubbed the scope check out
    entirely. The two guards log different lines, so the line is the only
    evidence that distinguishes them.

    Breaks on: ``_apply_site_plan`` losing its ``is_org_scoped`` guard.
    """
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: f"prod_{key}")
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.sites.service"):
        doc = await _publish(ws, pocket_id, org_key)

    persisted = await Site.find_one(Site.id == doc.id)
    assert persisted.plan_tier == site_plans.BASE_SITE_PLAN_KEY
    assert persisted.subscription_status != "active"
    # ``getMessage()``, not ``.message``: the latter is only populated once a
    # Formatter has run, so on a raw captured record it is either absent or the
    # unformatted template — and ``template % args`` blows up on a record whose
    # args were already applied.
    assert any("org-wide flat" in r.getMessage() for r in caplog.records), (
        "the tier was refused, but not BY THE SCOPE CHECK — see the docstring"
    )


async def test_an_org_flat_cannot_downgrade_a_site_that_is_already_paying(mongo_db, monkeypatch):
    """The refusal must fall back to the site's OWN tier, not to the floor.

    An org key arriving on a republish is a bad request, and punishing a paying
    site for it — by resetting ``plan_tier`` to free and dropping its badge
    removal — turns a rejected upgrade into a silent downgrade. Same rule the
    unknown-key and unpurchasable-tier paths already follow.
    """
    monkeypatch.setattr(site_plans, "_dodo_product_for", lambda key: f"prod_{key}")
    ws = await _make_workspace()
    pocket_id = await _make_pocket(workspace_id=ws)

    doc = await _publish(ws, pocket_id, None)
    persisted = await Site.find_one(Site.id == doc.id)
    persisted.plan_tier = "site"
    persisted.subscription_status = "active"
    persisted.subscription_id = "sub_existing"
    await persisted.save()

    await _publish(ws, pocket_id, "studio")

    after = await Site.find_one(Site.id == doc.id)
    assert after.plan_tier == "site", "a rejected org key downgraded a paying site"


async def test_the_catalog_endpoint_offers_org_flats_but_the_publish_path_does_not(mongo_db):
    """The two lists exist for different callers and must not be confused.

    The storefront reads the whole catalog — it has to render Studio and Agency to
    sell them. The publish path reads only the per-site rungs. If a picker were
    ever wired to ``list_site_plans`` it would offer a tier the backend refuses,
    which reads to the buyer as an upgrade that did nothing.
    """
    catalog_keys = {t.key for t in site_plans.list_site_plans()}
    publishable = {t.key for t in site_plans.list_site_scoped_plans()}

    assert {"studio", "agency"} <= catalog_keys
    assert not ({"studio", "agency"} & publishable)
    assert publishable < catalog_keys
