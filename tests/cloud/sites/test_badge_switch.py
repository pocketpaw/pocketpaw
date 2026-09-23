# tests/cloud/sites/test_badge_switch.py — the per-site "hide the PocketPaw badge"
# switch (VS-3), driven through ``sites.service.update_site_branding``.
#
# Created 2026-09-23 (feat/sites-badge-switch). What this pins:
#   * A site whose plan does not remove the badge cannot store badge_hidden=True:
#     402 ``billing.badge_removal_not_entitled`` and NOTHING written.
#   * badge_hidden=False is always accepted, entitled or not.
#   * A no-op PATCH (the value already stored) writes nothing: no ``set()`` call.
#     There is no redeploy on a toggle; the preference takes effect on the next
#     publish, which the end-to-end cases below exercise.
#   * A row with no ``badge_hidden`` in Mongo reads True (the old behaviour), and
#     a PATCH to True on it leaves the raw document untouched.
#   * End to end through ``publish``: an entitled live site's page follows the
#     flag, and the publish upsert keeps the owner's choice instead of resetting
#     it to the model default (mutation: ``doc.badge_hidden = True`` in the
#     upsert fails the False case).
#
# Tier keys come from the catalog, not literals, the same way
# test_custom_domain_entitlement.py does it, so a catalog rekey moves these tests
# instead of breaking them.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import BadgeRemovalNotEntitled  # noqa: E402
from pocketpaw_ee.cloud.billing import site_plans  # noqa: E402
from pocketpaw_ee.cloud.models.site import Site  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.dto import SiteBrandingUpdate  # noqa: E402

import pocketpaw.config as ppconfig  # noqa: E402

pytestmark = pytest.mark.usefixtures("mongo_db")


def _a_tier_that_removes_the_badge() -> str:
    for tier in site_plans.list_site_plans():
        if tier.badge_removal:
            return tier.key
    raise AssertionError("no site plan tier grants badge removal — catalog changed")


def _enforce(monkeypatch, *, on: bool) -> None:
    """Pin the billing posture. Without this, python-dotenv can climb out of the
    worktree and load the operator's live billing config."""
    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(
            billing_enforced=on, sites_billing_enforced=False, dodo_site_products=None
        ),
    )


@pytest.fixture
def writes(monkeypatch):
    """Record every ``Site.set()`` the branding service makes, then let it through.
    The service's only write path is a targeted ``set()``, so an empty list means
    nothing was written."""
    calls: list[dict] = []
    real_set = Site.set

    async def _spy(self, expression, *a, **kw):
        calls.append(dict(expression))
        return await real_set(self, expression, *a, **kw)

    monkeypatch.setattr(Site, "set", _spy)
    return calls


async def _seed(
    *,
    workspace_id: str,
    plan_tier: str | None,
    subscription_status: str = "none",
    deployed: bool = True,
    badge_hidden: bool = True,
) -> str:
    doc = Site(
        workspace=workspace_id,
        pocket_id="pk_1",
        owner="u1",
        name="My Site",
        plan_tier=plan_tier,
        subscription_status=subscription_status,
        deployed=deployed,
        badge_hidden=badge_hidden,
    )
    await doc.insert()
    return str(doc.id)


async def _patch(ws: str, site_id: str, hidden: bool):
    return await sites_service.update_site_branding(
        workspace_id=ws, site_id=site_id, body=SiteBrandingUpdate(badge_hidden=hidden)
    )


# --------------------------------------------------------------------------- #
# The plan gate.
# --------------------------------------------------------------------------- #


async def test_a_free_site_cannot_hide_the_badge(monkeypatch, writes):
    """402, the stored flag unchanged, and nothing written."""
    _enforce(monkeypatch, on=True)
    ws = "ws_badge_free"
    site_id = await _seed(
        workspace_id=ws, plan_tier=site_plans.BASE_SITE_PLAN_KEY, badge_hidden=False
    )

    with pytest.raises(BadgeRemovalNotEntitled) as exc:
        await _patch(ws, site_id, True)

    assert exc.value.status_code == 402
    assert exc.value.code == "billing.badge_removal_not_entitled"
    assert exc.value.to_dict() == {
        "error": {"code": "billing.badge_removal_not_entitled", "message": exc.value.message}
    }
    assert (await Site.get(site_id)).badge_hidden is False
    assert writes == []


@pytest.mark.parametrize("status", ["cancelled", "none", "pending"])
async def test_a_paid_tier_without_an_active_subscription_cannot_hide_it(
    monkeypatch, writes, status
):
    """A lapsed or never-charged paid tier keeps ``plan_tier``; reading the tier
    alone would let it flip the switch."""
    _enforce(monkeypatch, on=True)
    ws = f"ws_badge_lapsed_{status}"
    site_id = await _seed(
        workspace_id=ws,
        plan_tier=_a_tier_that_removes_the_badge(),
        subscription_status=status,
        badge_hidden=False,
    )

    with pytest.raises(BadgeRemovalNotEntitled) as exc:
        await _patch(ws, site_id, True)

    assert "renew" in exc.value.message
    assert (await Site.get(site_id)).badge_hidden is False
    assert writes == []


async def test_showing_the_badge_is_always_allowed(monkeypatch, writes):
    _enforce(monkeypatch, on=True)
    ws = "ws_badge_show_free"
    site_id = await _seed(workspace_id=ws, plan_tier=site_plans.BASE_SITE_PLAN_KEY)

    res = await _patch(ws, site_id, False)

    assert res.badge_hidden is False
    assert (await Site.get(site_id)).badge_hidden is False


async def test_an_entitled_site_can_hide_the_badge(monkeypatch, writes):
    _enforce(monkeypatch, on=True)
    ws = "ws_badge_entitled"
    site_id = await _seed(
        workspace_id=ws,
        plan_tier=_a_tier_that_removes_the_badge(),
        subscription_status="active",
        badge_hidden=False,
    )

    res = await _patch(ws, site_id, True)

    assert res.badge_hidden is True
    assert (await Site.get(site_id)).badge_hidden is True


async def test_self_host_stores_the_preference_without_a_paywall(monkeypatch, writes):
    """With billing off there is no 402; the stamper still badges a free site."""
    _enforce(monkeypatch, on=False)
    ws = "ws_badge_selfhost"
    site_id = await _seed(workspace_id=ws, plan_tier=None, badge_hidden=False)

    res = await _patch(ws, site_id, True)

    assert res.badge_hidden is True


# --------------------------------------------------------------------------- #
# No-op and tenancy.
# --------------------------------------------------------------------------- #


async def test_a_no_op_patch_writes_nothing(monkeypatch, writes):
    _enforce(monkeypatch, on=True)
    ws = "ws_badge_noop"
    site_id = await _seed(
        workspace_id=ws,
        plan_tier=_a_tier_that_removes_the_badge(),
        subscription_status="active",
        badge_hidden=True,
    )

    res = await _patch(ws, site_id, True)

    assert res.badge_hidden is True
    assert writes == []


async def test_a_cross_tenant_site_is_not_found(monkeypatch, writes):
    _enforce(monkeypatch, on=True)
    site_id = await _seed(workspace_id="ws_owner", plan_tier=None)

    with pytest.raises(Exception) as exc:
        await _patch("ws_stranger", site_id, False)

    assert getattr(exc.value, "status_code", None) == 404
    assert writes == []


# --------------------------------------------------------------------------- #
# Legacy rows.
# --------------------------------------------------------------------------- #


async def test_a_row_without_the_field_reads_hidden(monkeypatch, writes):
    """A document written before VS-3 has no ``badge_hidden`` in Mongo. It must read
    True, which is what an entitled site did before this switch existed, and a PATCH
    to True on it is a no-op rather than a change."""
    _enforce(monkeypatch, on=True)
    ws = "ws_badge_legacy"
    site_id = await _seed(
        workspace_id=ws,
        plan_tier=_a_tier_that_removes_the_badge(),
        subscription_status="active",
    )
    await Site.get_pymongo_collection().update_one(
        {"_id": (await Site.get(site_id)).id}, {"$unset": {"badge_hidden": ""}}
    )
    raw = await Site.get_pymongo_collection().find_one({"_id": (await Site.get(site_id)).id})
    assert "badge_hidden" not in raw

    assert (await Site.get(site_id)).badge_hidden is True
    res = await _patch(ws, site_id, True)
    assert res.badge_hidden is True
    assert writes == []
    raw = await Site.get_pymongo_collection().find_one({"_id": (await Site.get(site_id)).id})
    assert "badge_hidden" not in raw


def test_the_body_requires_the_flag():
    with pytest.raises(ValueError):
        SiteBrandingUpdate()


def test_the_response_carries_badge_hidden_rather_than_only_declaring_it():
    """``_to_response`` builds the DTO field by field; a declared-but-never-passed
    field reads its default forever."""
    import inspect

    assert "badge_hidden=" in inspect.getsource(sites_service._to_response)


# --------------------------------------------------------------------------- #
# End to end through a live publish.
# --------------------------------------------------------------------------- #


class _FakeGenerator:
    """Writes a one-page static tree so the stamper has something to walk."""

    def __init__(self, project_dir):
        self.project_dir = project_dir

    async def build(self, **kw):
        from pathlib import Path

        from pocketpaw_ee.sites.engines import static_output_rel
        from pocketpaw_ee.sites.generator_client import BuildResult

        out = Path(self.project_dir, static_output_rel("ripple"))
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")
        return BuildResult(project_dir=str(self.project_dir), ripple_version="0.2.0")


async def _publish_live(tmp_path, monkeypatch, ws: str, pocket_id: str):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")

    async def _no_bar(*_a, **_kw):
        return None

    monkeypatch.setattr(sites_service, "_embed_concierge_bar", _no_bar)
    monkeypatch.setattr(sites_service, "require_sites_plan", _no_bar)
    return await sites_service.publish(
        workspace_id=ws,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="My Site",
        _generator=_FakeGenerator(tmp_path / "project"),
        _cloudflare=None,
        _bundle_reader=lambda d: b"x",
        _local_deploy=lambda site_id, project_dir: "https://site-1.paw.dev",
    )


@pytest.mark.parametrize(("hidden", "badged"), [(False, True), (True, False)])
async def test_a_republish_honours_and_keeps_the_owners_choice(
    tmp_path, monkeypatch, hidden, badged
):
    """An entitled live site: the published page follows ``badge_hidden``, and the
    publish's upsert does not reset the preference to the model default."""
    from pocketpaw_ee.sites import badge

    ws, pocket_id = f"ws_badge_e2e_{hidden}", "pk_e2e"
    oid = await sites_service._resolve_live_site_oid(ws, pocket_id)
    await Site(
        id=oid,
        workspace=ws,
        pocket_id=pocket_id,
        owner="u1",
        name="My Site",
        plan_tier=_a_tier_that_removes_the_badge(),
        subscription_status="active",
        deployed=True,
        badge_hidden=hidden,
    ).insert()

    await _publish_live(tmp_path, monkeypatch, ws, pocket_id)

    page = next((tmp_path / "project").rglob("index.html")).read_text(encoding="utf-8")
    assert (badge.BADGE_MARKER in page) is badged
    assert (await Site.get(oid)).badge_hidden is hidden
