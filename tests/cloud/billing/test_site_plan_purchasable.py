# tests/cloud/billing/test_site_plan_purchasable.py — a per-site plan nobody can
# buy, and the setting that was read but never declared.
#
# Created 2026-08-21 (feat/site-plan-purchasable). Found while checking what a
# free workspace does after the custom-domain cap refuses it and the 402 says
# "upgrade a site's plan to connect another".
#
# It could not. ``site_plans._dodo_product_for`` resolves a tier's Dodo product
# with ``getattr(get_settings(), "dodo_site_products", None)`` and has done since
# per-site plans shipped — but ``dodo_site_products`` was never declared as a
# field on ``Settings``. So the getattr found nothing on every deployment,
# ``dodo_product_id`` was None for every tier, and setting
# POCKETPAW_DODO_SITE_PRODUCTS did precisely nothing. Not "unconfigured":
# unconfigurable.
#
# What that produced downstream, silently, is the part worth pinning. A paid tier
# with no product cannot open a checkout, so ``publish_pocket`` deliberately
# skips charge-first and publishes live rather than strand the user — recording
# ``plan_tier="pro"`` with ``subscription_status="none"``. Every entitlement then
# resolves that site as the free floor. The buyer picked a paid plan, paid
# nothing, received nothing, and the only trace was a server-side log line.
#
# ``purchasable`` gives that state a name the wire can carry, so a card can mark a
# tier unavailable instead of offering a button that quietly does nothing. It
# deliberately does NOT change what publish does — that fallback is a product
# decision, and this is the honest reporting of it.

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.entitlements.dto import site_plan_tier_to_dto  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402


def _products(monkeypatch, mapping: dict[str, str] | None) -> None:
    """Point the lazily-imported ``get_settings`` at a products stub.

    ``_dodo_product_for`` imports ``get_settings`` inside the call, so patching
    the module attribute reaches it — the same shape every other billing-posture
    stub in this tree uses.
    """
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(dodo_site_products=mapping),
    )


def _a_priced_tier() -> str:
    for tier in site_plans.list_site_plans():
        if tier.monthly_price_usd > 0:
            return tier.key
    raise AssertionError("no priced site tier in the catalog — ladder changed")


# --------------------------------------------------------------------------- #
# The setting exists at all. This is the whole bug.
# --------------------------------------------------------------------------- #


def test_the_setting_is_a_real_field():
    """``getattr(settings, "dodo_site_products", None)`` returned None on every
    deployment because the attribute did not exist. A test that only checked the
    getattr's default would have passed throughout."""
    from pocketpaw.config import Settings

    assert hasattr(Settings(), "dodo_site_products")


def test_the_env_var_now_reaches_the_catalog(monkeypatch):
    """End to end, through the real Settings rather than a stub: set the
    environment, and a tier acquires a product id. This is the assertion that was
    impossible to make yesterday."""
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_DODO_SITE_PRODUCTS", json.dumps({"pro": "prod_site_pro"}))

    assert Settings().dodo_site_products == {"pro": "prod_site_pro"}


def test_it_defaults_to_nothing_purchasable():
    """Empty is the correct default and the current state of every deployment."""
    from pocketpaw.config import Settings

    assert Settings().dodo_site_products == {}


@pytest.mark.parametrize("bad", ["not json", "[1,2,3]", '"a string"', ""])
def test_a_malformed_value_degrades_instead_of_failing_boot(monkeypatch, bad):
    """Mirrors ``dodo_plan_products``. A typo in an env var must not stop the
    server starting; it costs purchasability, which fails visibly at the card."""
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_DODO_SITE_PRODUCTS", bad)

    assert Settings().dodo_site_products == {}


# --------------------------------------------------------------------------- #
# purchasable — the name for a state that already existed and had none.
# --------------------------------------------------------------------------- #


def test_the_free_tier_is_always_purchasable(monkeypatch):
    """Nothing to buy, so selecting it always works. It must not read as
    unavailable just because it has no Dodo product."""
    _products(monkeypatch, None)

    floor = site_plans.get_site_plan(site_plans.BASE_SITE_PLAN_KEY)

    assert floor is not None
    assert floor.monthly_price_usd == 0
    assert floor.purchasable is True


def test_a_priced_tier_with_no_product_is_not_purchasable(monkeypatch):
    """The live state of every deployment. Selecting this tier publishes live,
    charges nothing and grants nothing."""
    _products(monkeypatch, None)

    assert site_plans.get_site_plan(_a_priced_tier()).purchasable is False


def test_a_priced_tier_becomes_purchasable_once_its_product_is_configured(monkeypatch):
    priced = _a_priced_tier()
    _products(monkeypatch, {priced: "prod_site_x"})

    tier = site_plans.get_site_plan(priced)

    assert tier.dodo_product_id == "prod_site_x"
    assert tier.purchasable is True


def test_configuring_one_tier_does_not_make_its_siblings_purchasable(monkeypatch):
    """Partial configuration is the likely real state during a rollout, and the
    card has to be right about each tier independently."""
    priced = [t.key for t in site_plans.list_site_plans() if t.monthly_price_usd > 0]
    if len(priced) < 2:
        pytest.skip("catalog has fewer than two priced tiers")
    _products(monkeypatch, {priced[0]: "prod_only_the_first"})

    assert site_plans.get_site_plan(priced[0]).purchasable is True
    assert site_plans.get_site_plan(priced[1]).purchasable is False


def test_a_junk_mapping_leaves_everything_unpurchasable(monkeypatch):
    """``_dodo_product_for`` guards a non-dict and a non-string value. Neither may
    resolve to a truthy product id, because a truthy junk id would send the
    publish down charge-first and fail against Dodo instead of degrading."""
    _products(monkeypatch, "not a mapping")  # type: ignore[arg-type]

    assert site_plans.get_site_plan(_a_priced_tier()).purchasable is False


# --------------------------------------------------------------------------- #
# On the wire, which is the point — the card cannot know otherwise.
# --------------------------------------------------------------------------- #


def test_the_dto_carries_purchasable(monkeypatch):
    _products(monkeypatch, None)

    rows = {t.key: site_plan_tier_to_dto(t) for t in site_plans.list_site_plans()}

    assert rows[site_plans.BASE_SITE_PLAN_KEY].purchasable is True
    assert rows[_a_priced_tier()].purchasable is False


def test_the_dto_follows_configuration(monkeypatch):
    priced = _a_priced_tier()
    _products(monkeypatch, {priced: "prod_site_x"})

    assert site_plan_tier_to_dto(site_plans.get_site_plan(priced)).purchasable is True


def test_purchasable_is_not_a_per_site_entitlement(monkeypatch):
    """Same caution the DTO's docstring gives ``badge_removal`` and
    ``sells_concierge``: this says what the CATALOG can sell, never what a
    particular site has. A purchasable tier still grants a given site nothing
    until that site's own subscription is active."""
    priced = _a_priced_tier()
    _products(monkeypatch, {priced: "prod_site_x"})

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
# The sibling setting had the same hole, and its docstring denied it.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "var",
    ["POCKETPAW_DODO_SITE_PRODUCTS", "POCKETPAW_DODO_PLAN_PRODUCTS"],
)
def test_neither_product_map_can_take_the_server_down_at_boot(monkeypatch, var):
    """``dodo_plan_products`` carried a before-validator and a docstring promising
    "a typo can't crash settings load" since 2026-06-24. It could.

    ``EnvSettingsSource`` JSON-decodes a complex field's raw value at SOURCE time
    and raises ``SettingsError`` on failure, before any field validator runs — so
    the validator was unreachable for exactly the input it existed to absorb. Both
    fields carry ``NoDecode`` now, which is what hands the raw string to the
    validator instead. Parametrised across both because the new field inherited
    the bug by copying the pattern, and fixing only the copy would leave the
    original armed.
    """
    from pocketpaw.config import Settings

    monkeypatch.setenv(var, "{not json at all")

    Settings()  # must not raise


# --------------------------------------------------------------------------- #
# The publish stops recording a tier the site does not hold.
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("mongo_db")
class TestThePublishRecordsWhatIsTrue:
    """``_apply_site_plan`` used to stamp whatever tier was asked for, purchasable
    or not, and every entitlement then read that site as the free floor anyway.

    The site still publishes and still goes live — that fallback exists so a buyer
    is never stranded by unconfigured billing, and it stays. What changes is that
    ``plan_tier`` stops claiming a plan nobody was charged for.
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

    async def test_an_unpurchasable_paid_tier_is_not_recorded(self, monkeypatch):
        """The headline. Picking a paid tier nobody can buy leaves the site on the
        floor rather than on a paid key with nothing behind it."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        _products(monkeypatch, None)
        doc = await self._seed("ws_unbuyable")

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_unbuyable",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=_a_priced_tier(),
            provider=None,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == site_plans.BASE_SITE_PLAN_KEY
        assert refreshed.subscription_status == "none"

    async def test_a_purchasable_tier_is_recorded_as_before(self, monkeypatch):
        """The guard must not become the bug: configure the product and the tier
        lands exactly as it always did."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        priced = _a_priced_tier()
        _products(monkeypatch, {priced: "prod_site_x"})
        doc = await self._seed("ws_buyable")

        class _Prov:
            async def create_subscription(self, **kw):
                return SimpleNamespace(subscription_id="sub_1", checkout_url=None)

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_buyable",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=priced,
            provider=_Prov(),
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == priced
        assert refreshed.subscription_status == "pending"

    async def test_it_never_downgrades_a_site_that_already_has_a_tier(self, monkeypatch):
        """Falls back to the site's EXISTING tier, not blindly to the floor.

        A site already holding a tier must not lose it because someone asked for a
        different, unbuyable one — that would turn a misconfigured product id into
        a silent downgrade of a paying customer.
        """
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        priced = [t.key for t in site_plans.list_site_plans() if t.monthly_price_usd > 0]
        if len(priced) < 2:
            pytest.skip("catalog has fewer than two priced tiers")
        _products(monkeypatch, {priced[0]: "prod_site_x"})
        doc = await self._seed("ws_no_downgrade", plan_tier=priced[0])

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_no_downgrade",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=priced[1],  # unbuyable
            provider=None,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == priced[0]

    async def test_an_explicit_move_to_free_still_works(self, monkeypatch):
        """$0 is purchasable by definition, so the guard must not block a
        downgrade."""
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.sites import service as svc

        _products(monkeypatch, None)
        doc = await self._seed("ws_to_free", plan_tier=_a_priced_tier())

        await svc._apply_site_plan(
            doc=doc,
            workspace_id="ws_to_free",
            user_id="u1",
            pocket_id="pk_1",
            site_plan_key=site_plans.BASE_SITE_PLAN_KEY,
            provider=None,
        )

        refreshed = await Site.get(str(doc.id))
        assert refreshed.plan_tier == site_plans.BASE_SITE_PLAN_KEY
