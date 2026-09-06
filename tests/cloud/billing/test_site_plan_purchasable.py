# tests/cloud/billing/test_site_plan_purchasable.py — what a site plan RECORDS,
# and what ``purchasable`` means now that no site plan touches a gateway.
#
# Created 2026-08-21 (feat/site-plan-purchasable) around a defect that no longer
# exists, and worth keeping the history because the shape recurs. A per-site tier
# was only purchasable when a Dodo product was configured for it; the setting
# that carried those ids was READ but never DECLARED, so the read found nothing
# on every deployment however the environment was set. Not "unconfigured":
# unconfigurable.
#
# What that produced downstream was silent all the way. A paid tier with no
# product could not open a checkout, so the publish went live and recorded the
# FLOOR — deliberately, since a tier the site cannot back is worse than no tier.
# The buyer selected a paid plan, was charged nothing, received nothing, and the
# card afterwards said the tier below. No error was raised anywhere in that
# sequence, and only a server-side log line mentioned it.
#
# REWRITTEN 2026-09-05 (fix/sites-plan-credits). Paw Sites left Dodo: a paid site
# is charged to the WORKSPACE CREDIT BALANCE, the two site product/add-on maps
# are deleted from Settings, and the ids are gone from the catalog. So the entire
# "unbuyable because unconfigured" state — and every case that measured it — is
# gone with them.
#
# TWO THINGS SURVIVE, and this module is now about those:
#
#   * ``purchasable`` still exists and still refuses something: an ORG FLAT,
#     which covers a whole workspace and cannot be bought one site at a time. It
#     answers a question about SCOPE now rather than about configuration.
#   * ``_apply_site_plan``'s FALLBACK LADDER, which is the part that was actually
#     protecting customers. A tier key it cannot record falls back to the site's
#     EXISTING tier first and only then to the floor, so one bad key can never
#     silently downgrade a paying customer. Its trigger changed from "priced but
#     unconfigured" to "not in the catalog"; the rule did not.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.entitlements.dto import site_plan_tier_to_dto  # noqa: E402


def _a_priced_tier() -> str:
    for tier in site_plans.list_site_plans():
        if tier.monthly_price_usd > 0 and not tier.is_org_scoped:
            return tier.key
    raise AssertionError("no priced per-site tier in the catalog — ladder changed")


# --------------------------------------------------------------------------- #
# The site product maps are GONE, not merely unread.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["dodo_site_products", "dodo_site_addons"])
def test_the_site_gateway_maps_are_gone_from_settings(name):
    """Deleted rather than left in place returning ``{}``.

    A setting nothing consumes is one a future change quietly depends on again,
    and these two in particular carried a rule — "this tier can be bought" — that
    has moved somewhere else entirely. Leaving them declared would let an operator
    set POCKETPAW_DODO_SITE_PRODUCTS, see it accepted, and reasonably conclude
    that site plans bill through Dodo.

    ``dodo_plan_products`` is deliberately NOT in this list: workspace plans still
    bill through the gateway, and that map is still live.
    """
    from pocketpaw.config import Settings

    assert not hasattr(Settings(), name)


def test_the_workspace_plan_map_is_untouched():
    """The other half of the assertion above. Removing the SITE maps must not take
    the workspace-plan one with it — a test that only checked for absence would
    pass just as happily if every Dodo setting had been deleted."""
    from pocketpaw.config import Settings

    assert hasattr(Settings(), "dodo_plan_products")


def test_the_catalog_carries_no_gateway_ids():
    """The field, not just the value. ``dodo_product_id = None`` on every tier
    would read identically to "unconfigured" — the exact ambiguity that hid the
    original bug for months — so what is asserted is that the attribute does not
    exist at all."""
    tier = site_plans.get_site_plan(_a_priced_tier())

    assert not hasattr(tier, "dodo_product_id")
    assert not hasattr(tier, "dodo_addon_id")


# --------------------------------------------------------------------------- #
# purchasable — the rule that survived, meaning something else.
# --------------------------------------------------------------------------- #


def test_the_free_tier_is_purchasable():
    """Nothing to buy, so selecting it always works."""
    floor = site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY)

    assert floor is not None
    assert floor.monthly_price_usd == 0
    assert floor.purchasable is True


def test_every_priced_per_site_rung_is_purchasable():
    """THE INVERSION. This used to be the headline failure — a priced tier with no
    product was unbuyable, on every deployment, forever. The credit balance can
    pay for any rung and needs no configuration, so being priced is now the whole
    of being buyable."""
    for tier in site_plans.list_site_scoped_plans():
        assert tier.purchasable is True, f"{tier.key} is not on sale"


@pytest.mark.parametrize("key", ["studio", "agency"])
def test_an_org_flat_is_not_purchasable(key):
    """The one row that still refuses, and the reason ``purchasable`` survives
    rather than being deleted with the rule that motivated it.

    An org flat covers a whole workspace. The per-site purchase buys one site and
    debits one site's price, so letting one through would take the price of a site
    and hand over an org plan's worth of claims."""
    assert site_plans.get_site_plan(key).purchasable is False


def test_the_dto_carries_purchasable():
    """The card cannot know otherwise. Both sides asserted, so this measures the
    scope rule rather than a blanket answer in either direction."""
    rows = {t.key: site_plan_tier_to_dto(t) for t in site_plans.list_site_plans()}

    assert rows[site_plans.BASE_SITE_PLAN_KEY].purchasable is True
    assert rows[_a_priced_tier()].purchasable is True
    org = [t.key for t in site_plans.list_site_plans() if t.is_org_scoped]
    assert org, "the catalog lost its org flats — the assertion below is vacuous"
    assert all(rows[k].purchasable is False for k in org)


def test_purchasable_is_not_a_per_site_entitlement():
    """Same caution the DTO's docstring gives ``badge_removal`` and
    ``sells_concierge``: this says what the CATALOG sells, never what a particular
    site has. A purchasable tier still grants a given site nothing until that
    site's own subscription is active."""
    priced = _a_priced_tier()

    from pocketpaw_ee.cloud.entitlements.service import resolve_site_entitlements

    ent = resolve_site_entitlements(
        site_id="6512c1f0e4b0a1b2c3d4e5f6",
        workspace_id="ws_1",
        plan_tier=priced,
        subscription_status="none",
        concierge_enabled=True,
    )

    assert site_plans.get_site_plan(priced).purchasable is True
    assert ent.subscription_active is False
    assert ent.badge_required is True


# --------------------------------------------------------------------------- #
# The setting that IS still a gateway map must not take the server down.
# --------------------------------------------------------------------------- #


def test_the_plan_product_map_cannot_take_the_server_down_at_boot(monkeypatch):
    """``dodo_plan_products`` carried a before-validator and a docstring promising
    "a typo can't crash settings load" since 2026-06-24. It could.

    ``EnvSettingsSource`` JSON-decodes a complex field's raw value at SOURCE time
    and raises ``SettingsError`` on failure, before any field validator runs — so
    the validator was unreachable for exactly the input it existed to absorb.
    ``NoDecode`` on the field is what hands the raw string to the validator
    instead.

    This used to be parametrised across the site map too. That map is gone; the
    trap it inherited by copying this pattern is worth remembering the next time
    somebody adds a JSON setting.
    """
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_DODO_PLAN_PRODUCTS", "{not json at all")

    Settings()  # must not raise


# --------------------------------------------------------------------------- #
# The publish records what is true — the fallback ladder, which is unchanged.
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("mongo_db")
class TestThePublishRecordsWhatIsTrue:
    """``_apply_site_plan`` used to stamp whatever tier was asked for and every
    entitlement then read that site as the free floor anyway.

    The RULE is unchanged: never record a tier the site does not hold, and when
    refusing one, fall back to the site's EXISTING tier before the floor — so a
    bad key can never silently downgrade a paying customer. Only the trigger
    changed, from "priced but unconfigured" (a state that no longer exists) to
    "not a key the catalog resolves".
    """

    @staticmethod
    async def _seed(workspace_id: str, plan_tier: str | None = None):
        from pocketpaw_ee.cloud.models.site import Site

        doc = Site(
            workspace=workspace_id,
            pocket_id="pk_1",
            owner="u1",
            name="My Site",
            plan_tier=plan_tier,
            subscription_status="none",
            deployed=True,
        )
        await doc.insert()
        return doc

    async def test_an_unrecordable_tier_is_not_recorded(self):
        """A tier key the catalog cannot resolve leaves the site on the floor
        rather than on a key with nothing behind it."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        doc = await self._seed("ws_unbuyable")

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_unbuyable",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key="a-tier-that-was-never-in-the-catalog",
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == site_plans.BASE_SITE_PLAN_KEY
        assert refreshed.subscription_status == "none"

    async def test_a_priced_tier_is_recorded(self):
        """The direct inversion of the case this module was created for, pinned so
        the change of rule is explicit rather than implied by a deleted test. The
        wallet bought this tier; refusing to record it would take the money and
        hand back the free floor."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        priced = _a_priced_tier()
        doc = await self._seed("ws_credits_paid")

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_credits_paid",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=priced,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == priced

    async def test_it_never_downgrades_a_site_that_already_has_a_tier(self):
        """Falls back to the site's EXISTING tier, not blindly to the floor.

        A site already holding a tier must not lose it because someone asked for a
        key the catalog cannot resolve — that would turn one bad string into a
        silent downgrade of a paying customer, and nothing restores a tier."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        held = _a_priced_tier()
        doc = await self._seed("ws_no_downgrade", plan_tier=held)

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_no_downgrade",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key="a-tier-that-was-never-in-the-catalog",
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == held

    async def test_an_explicit_move_to_free_still_works(self):
        """$0 is purchasable by definition, so the guard must not block a
        downgrade the customer actually asked for."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        doc = await self._seed("ws_to_free", plan_tier=_a_priced_tier())

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_to_free",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=site_plans.BASE_SITE_PLAN_KEY,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == site_plans.BASE_SITE_PLAN_KEY

    async def test_a_paying_site_keeps_its_status_through_a_republish(self):
        """The carve-out that stops a content edit stripping paid capabilities.

        Writing "none" over "active" during an unrelated republish would revoke
        badge removal, the custom domain and the concierge from a customer who is
        still being billed — and nothing restores it, because only a purchase or a
        renewal writes "active"."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        priced = _a_priced_tier()
        doc = await self._seed("ws_paying", plan_tier=priced)
        doc.subscription_status = "active"
        await doc.save()

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_paying",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=priced,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.subscription_status == "active"
        assert refreshed.plan_tier == priced


def test_the_dto_still_reports_the_catalog_claims():
    """A smoke check that stripping the gateway ids did not take the buyer-facing
    fields with them — the card is built from these."""
    row = site_plan_tier_to_dto(site_plans.get_site_plan(_a_priced_tier()))

    assert row.display_name
    assert row.monthly_price_usd > 0
    assert isinstance(row.cloudflare_features, list)
    assert isinstance(SimpleNamespace(**row.model_dump()).badge_removal, bool)
