# tests/ee/sites/test_sites_rename.py
# Created: 2026-09-23 (VS-4, feat/sites-rename) — an owner can check whether an address
# is free and rename a site's address. The rename is RESERVED now and goes live on the
# site's NEXT publish.
#
# The contract, in the order the sections below pin it:
#   * availability: one reason per way a name can be refused (invalid, reserved, the
#     ``paw-`` prefix, another site's slug or pending slug, a Worker in the account, a
#     hold from another workspace), the site's own names read as free, an own-workspace
#     or expired hold reads as free, and a taken name comes with a free suggestion;
#   * reserve: sets ``slug_pending``; 409 taken, 422 invalid, 429 on the 4th accepted
#     request in 24h; the current slug clears a pending rename; DELETE cancels;
#     cross-tenant is 404; two sites reserving one name at once -> exactly one wins;
#     a first publish claiming the name between a reserve's write and its verify makes
#     the reserve back off;
#   * the first-publish claim skips another site's pending name (and backs off when one
#     lands between its write and its verify) and another workspace's held name;
#   * apply at publish: routes re-pointed IN PLACE (PUT, same ids -- a second POST for
#     the same pattern is a Cloudflare 10020), old Worker deleted, doc updated, old slug
#     held; a failed re-point puts every route back, deletes the new Worker and keeps
#     ``slug_pending``; a failed deploy changes nothing; a failed old-Worker delete does
#     not fail the publish; a legacy ``paw-site-<id>`` rename holds nothing; a pending
#     name that appeared in the account is refused.
#
# MUTATIONS THAT BREAK THIS FILE: tests/mutations/sites_rename.json.
#
# Every test pins PAW_CF_DEPLOY_MODE and replaces ``account_script_names`` and the two
# Cloudflare client factories: python-dotenv can climb out of the worktree and load
# real PAW_CF_* values.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import ConflictError, RateLimited, ValidationError
from pocketpaw_ee.cloud.models.released_slug import ReleasedSlug
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.models.site import SiteDomain
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

WS = "ws-rename"
OTHER_WS = "ws-elsewhere"
OWNER = "u1"


# ── fakes and fixtures ───────────────────────────────────────────────────────


class _FakeCF:
    """Cloudflare as the rename sees it: routes by id, and the account's scripts."""

    def __init__(self, account: set[str]):
        self.account = account
        self.routes: dict[str, str] = {}
        self.log: list[tuple[str, ...]] = []
        self.fail_route: set[tuple[str, str]] = set()
        self.fail_delete: set[str] = set()

    async def update_worker_route(self, route_id, *, pattern, script):
        self.log.append(("route", route_id, script))
        if (route_id, script) in self.fail_route:
            raise RuntimeError(f"cloudflare refused route {route_id}")
        self.routes[route_id] = script

    async def delete_account_script(self, name):
        self.log.append(("delete", name))
        if name in self.fail_delete:
            raise RuntimeError(f"cloudflare refused to delete {name}")
        self.account.discard(name)

    def deleted(self) -> list[str]:
        return [e[1] for e in self.log if e[0] == "delete" and e[1] not in self.fail_delete]


@pytest.fixture
def account(monkeypatch):
    """The Cloudflare account's Worker scripts, as every read path sees them."""
    scripts: set[str] = set()

    async def _names(*, refresh: bool = False) -> frozenset[str]:
        return frozenset(scripts)

    monkeypatch.setattr(sites_service, "account_script_names", _names)
    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    return scripts


@pytest.fixture
def cf(account, monkeypatch) -> _FakeCF:
    fake = _FakeCF(account)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: fake)
    monkeypatch.setattr(sites_service, "_cf_account_client", lambda: fake)
    return fake


async def _seed_live(
    pocket_id: str,
    *,
    slug: str | None = "acme-bakery",
    workspace: str = WS,
    hosts: tuple[str, ...] = (),
    pending: str | None = None,
    changes: list[datetime] | None = None,
) -> ObjectId:
    """A published ``workers`` site. ``slug=None`` is a legacy ``paw-site-<id>`` site."""
    oid = sites_service._live_object_id(workspace, pocket_id)
    host = slug or f"paw-site-{oid}"
    await _SiteDoc(
        id=oid,
        workspace=workspace,
        pocket_id=pocket_id,
        owner=OWNER,
        name="Acme Bakery",
        script_name=str(oid),
        deployed=True,
        deploy_target="workers",
        url=f"https://{host}.acct.workers.dev",
        slug=slug,
        worker_name=slug,
        slug_pending=pending,
        slug_changes=changes or [],
        domains=[
            SiteDomain(hostname=h, cf_hostname_id=f"ch-{i}", cf_route_id=f"r{i}", status="live")
            for i, h in enumerate(hosts)
        ],
    ).insert()
    return oid


async def _hold(slug: str, *, workspace: str, days: int = 30) -> None:
    now = datetime.now(UTC)
    await ReleasedSlug(
        slug=slug,
        site_id="old-site",
        workspace_id=workspace,
        released_at=now,
        hold_until=now + timedelta(days=days),
    ).insert()


async def _row(oid) -> dict:
    # Re-read from the collection: mongomock hands back docs by reference.
    return await _SiteDoc.get_pymongo_collection().find_one({"_id": ObjectId(str(oid))})


async def _holds() -> list[dict]:
    return await ReleasedSlug.get_pymongo_collection().find({}).to_list(None)


async def _available(raw: str, *, workspace: str = WS, site_id: Any = None):
    return await sites_service.check_slug_availability(
        workspace_id=workspace, raw=raw, site_id=str(site_id) if site_id else None
    )


async def _reserve(oid, raw: str, *, workspace: str = WS):
    return await sites_service.reserve_slug_rename(
        workspace_id=workspace, site_id=str(oid), raw=raw
    )


# ── availability ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "normalized", "reason"),
    [
        ("a", "a", "invalid"),
        ("!!!", "", "invalid"),
        ("admin", "admin", "reserved"),
        ("www2", "www2", "reserved"),
        ("paw-sites-dispatch", "paw-sites-dispatch", "reserved"),
        ("Paw Site Extra", "paw-site-extra", "reserved"),
    ],
)
async def test_availability_refuses_bad_shapes(beanie_test_db, account, raw, normalized, reason):
    got = await _available(raw)

    assert (got.available, got.normalized, got.reason) == (False, normalized, reason)


@pytest.mark.asyncio
async def test_a_free_name_is_available_and_normalized(beanie_test_db, account):
    got = await _available("Acme Cakes!")

    assert (got.available, got.normalized, got.reason, got.suggestion) == (
        True,
        "acme-cakes",
        None,
        None,
    )


@pytest.mark.asyncio
async def test_another_sites_slug_is_taken_with_a_suggestion(beanie_test_db, account):
    await _seed_live("pk-a", slug="acme-cakes", workspace=OTHER_WS)

    got = await _available("acme-cakes")

    assert (got.available, got.reason) == (False, "taken")
    assert got.suggestion == "acme-cakes-2"


@pytest.mark.asyncio
async def test_another_sites_pending_slug_is_taken(beanie_test_db, account):
    await _seed_live("pk-a", slug="acme-one", workspace=OTHER_WS, pending="acme-cakes")

    got = await _available("acme-cakes")

    assert (got.available, got.reason) == (False, "taken")


@pytest.mark.asyncio
async def test_a_worker_in_the_account_is_taken(beanie_test_db, account):
    account.add("acme-cakes")

    got = await _available("acme-cakes")

    assert (got.available, got.reason, got.suggestion) == (False, "taken", "acme-cakes-2")


@pytest.mark.asyncio
async def test_a_name_held_by_another_workspace_is_held(beanie_test_db, account):
    await _hold("acme-cakes", workspace=OTHER_WS)

    got = await _available("acme-cakes")

    assert (got.available, got.reason) == (False, "held")


@pytest.mark.asyncio
async def test_a_name_held_by_your_own_workspace_is_available(beanie_test_db, account):
    await _hold("acme-cakes", workspace=WS)

    got = await _available("acme-cakes")

    assert (got.available, got.reason) == (True, None)


@pytest.mark.asyncio
async def test_an_expired_hold_is_available(beanie_test_db, account):
    await _hold("acme-cakes", workspace=OTHER_WS, days=-1)

    got = await _available("acme-cakes")

    assert (got.available, got.reason) == (True, None)


@pytest.mark.asyncio
async def test_a_sites_own_slug_and_pending_slug_read_as_available(beanie_test_db, account):
    oid = await _seed_live("pk-own", slug="acme-bakery", pending="acme-cakes")
    account.add("acme-bakery")  # its own live Worker

    assert (await _available("acme-bakery", site_id=oid)).available is True
    assert (await _available("acme-cakes", site_id=oid)).available is True
    # ...but only to that site.
    assert (await _available("acme-bakery")).reason == "taken"


@pytest.mark.asyncio
async def test_availability_for_a_foreign_site_id_is_404(beanie_test_db, account):
    from pocketpaw_ee.cloud._core.errors import NotFound

    oid = await _seed_live("pk-foreign", workspace=OTHER_WS)

    with pytest.raises(NotFound):
        await _available("anything-free", site_id=oid)


# ── reserve / cancel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_sets_slug_pending_and_keeps_the_live_address(beanie_test_db, account):
    oid = await _seed_live("pk-r")

    resp = await _reserve(oid, "Acme Cakes")
    row = await _row(oid)

    assert (resp.slug, resp.slug_pending) == ("acme-bakery", "acme-cakes")
    assert (row["slug"], row["worker_name"], row["slug_pending"]) == (
        "acme-bakery",
        "acme-bakery",
        "acme-cakes",
    )
    assert len(row["slug_changes"]) == 1


@pytest.mark.asyncio
async def test_reserve_a_taken_name_is_409(beanie_test_db, account):
    await _seed_live("pk-holder", slug="acme-cakes", workspace=OTHER_WS)
    oid = await _seed_live("pk-r")

    with pytest.raises(ConflictError) as exc:
        await _reserve(oid, "acme-cakes")

    assert exc.value.code == "sites.slug_taken"
    assert (await _row(oid))["slug_pending"] is None


@pytest.mark.asyncio
async def test_reserve_a_held_name_is_409(beanie_test_db, account):
    await _hold("acme-cakes", workspace=OTHER_WS)
    oid = await _seed_live("pk-r")

    with pytest.raises(ConflictError) as exc:
        await _reserve(oid, "acme-cakes")

    assert exc.value.code == "sites.slug_held"


@pytest.mark.asyncio
async def test_reserve_an_invalid_name_is_422_and_a_reserved_one_409(beanie_test_db, account):
    oid = await _seed_live("pk-r")

    with pytest.raises(ValidationError) as bad:
        await _reserve(oid, "x")
    with pytest.raises(ConflictError) as reserved:
        await _reserve(oid, "paw-mine")

    assert bad.value.code == "sites.slug_invalid"
    assert reserved.value.code == "sites.slug_reserved"


@pytest.mark.asyncio
async def test_the_fourth_rename_in_24h_is_429(beanie_test_db, account):
    oid = await _seed_live("pk-r")
    for name in ("acme-one", "acme-two", "acme-three"):
        await _reserve(oid, name)

    with pytest.raises(RateLimited) as exc:
        await _reserve(oid, "acme-four")

    assert exc.value.code == "sites.slug_rate_limited"
    assert (await _row(oid))["slug_pending"] == "acme-three"


@pytest.mark.asyncio
async def test_renames_older_than_24h_do_not_count(beanie_test_db, account):
    old = datetime.now(UTC) - timedelta(hours=25)
    oid = await _seed_live("pk-r", changes=[old, old, old])

    await _reserve(oid, "acme-cakes")

    assert len((await _row(oid))["slug_changes"]) == 1


@pytest.mark.asyncio
async def test_asking_for_the_current_slug_clears_the_pending_rename(beanie_test_db, account):
    oid = await _seed_live("pk-r", pending="acme-cakes")

    resp = await _reserve(oid, "acme-bakery")

    assert resp.slug_pending is None
    assert (await _row(oid))["slug_pending"] is None


@pytest.mark.asyncio
async def test_asking_for_the_pending_slug_again_is_a_no_op(beanie_test_db, account):
    oid = await _seed_live("pk-r")
    await _reserve(oid, "acme-cakes")

    resp = await _reserve(oid, "acme-cakes")

    assert resp.slug_pending == "acme-cakes"
    assert len((await _row(oid))["slug_changes"]) == 1


@pytest.mark.asyncio
async def test_delete_cancels_a_pending_rename(beanie_test_db, account):
    oid = await _seed_live("pk-r", pending="acme-cakes")

    resp = await sites_service.cancel_slug_rename(workspace_id=WS, site_id=str(oid))

    assert resp.slug_pending is None
    assert (await _row(oid))["slug_pending"] is None


@pytest.mark.asyncio
async def test_reserve_is_refused_off_the_workers_lane(beanie_test_db, account, monkeypatch):
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "wfp")
    oid = await _seed_live("pk-r")

    with pytest.raises(ConflictError) as exc:
        await _reserve(oid, "acme-cakes")

    assert exc.value.code == "sites.slug_unsupported_lane"


@pytest.mark.asyncio
async def test_reserve_is_refused_for_a_never_published_site(beanie_test_db, account):
    oid = sites_service._live_object_id(WS, "pk-draft")
    await _SiteDoc(id=oid, workspace=WS, pocket_id="pk-draft", owner=OWNER, name="Draft").insert()

    with pytest.raises(ConflictError) as exc:
        await _reserve(oid, "acme-cakes")

    assert exc.value.code == "sites.slug_needs_publish"


# ── the races ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_sites_reserving_one_name_at_once_exactly_one_wins(
    beanie_test_db, account, monkeypatch
):
    """Both pass the availability read (it cannot see the other's write yet); the
    partial unique index on ``slug_pending`` lets exactly one of them keep it."""
    a = await _seed_live("pk-a", slug="acme-a")
    b = await _seed_live("pk-b", slug="acme-b", workspace=OTHER_WS)

    async def _blind(*_a, **_k):
        return None

    monkeypatch.setattr(sites_service, "_slug_unavailable_reason", _blind)

    results = await asyncio.gather(
        _reserve(a, "acme-cakes"),
        _reserve(b, "acme-cakes", workspace=OTHER_WS),
        return_exceptions=True,
    )

    losers = [r for r in results if isinstance(r, Exception)]
    assert len(losers) == 1
    assert isinstance(losers[0], ConflictError) and losers[0].code == "sites.slug_taken"
    pending = await _SiteDoc.get_pymongo_collection().count_documents(
        {"slug_pending": "acme-cakes"}
    )
    assert pending == 1


def _interleave(monkeypatch, *, when_field: str, competitor):
    """Run ``competitor`` exactly between a writer's write and its verify read of
    ``when_field``: the window write-then-verify exists to close."""
    real = sites_service._address_claimed_by_other_site
    fired: list[bool] = []

    async def _verify(field, slug, oid):
        if field == when_field and not fired:
            fired.append(True)
            await competitor(slug)
        return await real(field, slug, oid)

    monkeypatch.setattr(sites_service, "_address_claimed_by_other_site", _verify)
    return fired


@pytest.mark.asyncio
async def test_a_reserve_backs_off_when_a_first_publish_claims_the_name_mid_write(
    beanie_test_db, account, monkeypatch
):
    oid = await _seed_live("pk-r")

    async def _first_publish_claims(slug):
        await _seed_live("pk-new", slug=slug, workspace=OTHER_WS)

    fired = _interleave(monkeypatch, when_field="slug", competitor=_first_publish_claims)

    with pytest.raises(ConflictError) as exc:
        await _reserve(oid, "acme-cakes")

    assert fired
    assert exc.value.code == "sites.slug_taken"
    row = await _row(oid)
    assert row["slug_pending"] is None
    assert row["slug_changes"] == []


# ── the first-publish claim (VS-2) meets pending and held names ──────────────


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _Deployer:
    def __init__(self, fail: bool = False):
        self.names: list[str | None] = []
        self.fail = fail

    async def __call__(self, site_id, project_dir, *, worker_name=None, **_):
        self.names.append(worker_name)
        if self.fail:
            raise RuntimeError("wrangler: deploy failed")
        return f"https://{worker_name}.acct.workers.dev"


async def _publish(pocket_id: str, deployer, *, name: str = "Acme Cakes", workspace=WS):
    return await sites_service.publish(
        workspace_id=workspace,
        user_id=OWNER,
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name=name,
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=deployer,
    )


@pytest.mark.asyncio
async def test_a_first_publish_skips_another_sites_pending_name(beanie_test_db, account):
    await _seed_live("pk-renamer", slug="acme-one", workspace=OTHER_WS, pending="acme-cakes")
    deploy = _Deployer()

    site = await _publish("pk-new", deploy)

    assert deploy.names == ["acme-cakes-2"]
    assert (await _row(site.id))["slug"] == "acme-cakes-2"


@pytest.mark.asyncio
async def test_a_first_publish_backs_off_when_a_rename_reserves_the_name_mid_write(
    beanie_test_db, account, monkeypatch
):
    renamer = await _seed_live("pk-renamer", slug="acme-one", workspace=OTHER_WS)

    async def _rename_reserves(slug):
        await _SiteDoc.get_pymongo_collection().update_one(
            {"_id": renamer}, {"$set": {"slug_pending": slug}}
        )

    fired = _interleave(monkeypatch, when_field="slug_pending", competitor=_rename_reserves)
    deploy = _Deployer()

    site = await _publish("pk-new", deploy)

    assert fired
    assert deploy.names == ["acme-cakes-2"]
    assert (await _row(site.id))["slug"] == "acme-cakes-2"
    assert (await _row(renamer))["slug_pending"] == "acme-cakes"


@pytest.mark.asyncio
async def test_a_first_publish_skips_a_name_another_workspace_holds(beanie_test_db, account):
    await _hold("acme-cakes", workspace=OTHER_WS)
    deploy = _Deployer()

    await _publish("pk-new", deploy)

    assert deploy.names == ["acme-cakes-2"]


@pytest.mark.asyncio
async def test_a_first_publish_reclaims_its_own_workspaces_hold(beanie_test_db, account):
    await _hold("acme-cakes", workspace=WS)
    deploy = _Deployer()

    await _publish("pk-new", deploy)

    assert deploy.names == ["acme-cakes"]
    assert await _holds() == []


# ── apply at the next publish ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rename_moves_the_worker_and_both_domains(beanie_test_db, cf):
    oid = await _seed_live("pk-r", hosts=("acme.com", "www.acme.com"), pending="acme-cakes")
    cf.account.add("acme-bakery")
    cf.routes.update({"r0": "acme-bakery", "r1": "acme-bakery"})
    deploy = _Deployer()

    await _publish("pk-r", deploy)
    row = await _row(oid)

    assert deploy.names == ["acme-cakes"]
    # Both routes now answer from the new Worker, re-pointed in place (same ids)...
    assert cf.routes == {"r0": "acme-cakes", "r1": "acme-cakes"}
    assert [d["cf_route_id"] for d in row["domains"]] == ["r0", "r1"]
    # ...and only then was the old Worker deleted.
    assert cf.log[-1] == ("delete", "acme-bakery")
    assert cf.deleted() == ["acme-bakery"]
    assert (row["slug"], row["worker_name"], row["slug_pending"]) == (
        "acme-cakes",
        "acme-cakes",
        None,
    )
    assert row["url"] == "https://acme-cakes.acct.workers.dev"
    assert "acme-cakes.acct.workers.dev" in row["allowed_origins"]
    holds = await _holds()
    assert [(h["slug"], h["workspace_id"], h["site_id"]) for h in holds] == [
        ("acme-bakery", WS, str(oid))
    ]
    held_for = holds[0]["hold_until"] - holds[0]["released_at"]
    assert held_for == timedelta(days=30)


@pytest.mark.asyncio
async def test_a_failed_re_point_puts_everything_back(beanie_test_db, cf):
    oid = await _seed_live("pk-r", hosts=("acme.com", "www.acme.com"), pending="acme-cakes")
    before = await _row(oid)
    cf.routes.update({"r0": "acme-bakery", "r1": "acme-bakery"})
    cf.fail_route.add(("r1", "acme-cakes"))

    with pytest.raises(RuntimeError, match="refused route r1"):
        await _publish("pk-r", _Deployer())
    row = await _row(oid)

    # Every route answers from the old Worker again, and the old Worker still exists.
    assert cf.routes == {"r0": "acme-bakery", "r1": "acme-bakery"}
    assert cf.deleted() == ["acme-cakes"]
    # The row is exactly as it was: still serving at the old name, rename still waiting.
    for field in ("slug", "worker_name", "url", "slug_pending", "domains", "deployed_at"):
        assert row[field] == before[field], field
    assert await _holds() == []


@pytest.mark.asyncio
async def test_a_failed_deploy_changes_nothing(beanie_test_db, cf):
    oid = await _seed_live("pk-r", hosts=("acme.com",), pending="acme-cakes")
    before = await _row(oid)

    with pytest.raises(Exception, match="wrangler"):
        await _publish("pk-r", _Deployer(fail=True))
    row = await _row(oid)

    assert not [e for e in cf.log if e[0] == "route"]
    assert "acme-bakery" not in cf.deleted()
    for field in ("slug", "worker_name", "url", "slug_pending", "domains"):
        assert row[field] == before[field], field
    assert await _holds() == []


@pytest.mark.asyncio
async def test_a_failed_old_worker_delete_does_not_fail_the_publish(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.fail_delete.add("acme-bakery")

    await _publish("pk-r", _Deployer())
    row = await _row(oid)

    assert (row["slug"], row["slug_pending"], row["deployed"]) == ("acme-cakes", None, True)
    assert [h["slug"] for h in await _holds()] == ["acme-bakery"]


@pytest.mark.asyncio
async def test_a_legacy_site_renames_and_holds_nothing(beanie_test_db, cf):
    oid = await _seed_live("pk-legacy", slug=None, pending="acme-cakes")
    deploy = _Deployer()

    await _publish("pk-legacy", deploy)
    row = await _row(oid)

    assert deploy.names == ["acme-cakes"]
    assert (row["slug"], row["worker_name"]) == ("acme-cakes", "acme-cakes")
    assert cf.deleted() == [f"paw-site-{oid}"]
    assert await _holds() == []


@pytest.mark.asyncio
async def test_a_pending_name_that_appeared_in_the_account_is_refused(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.account.add("acme-cakes")
    deploy = _Deployer()

    with pytest.raises(ConflictError) as exc:
        await _publish("pk-r", deploy)

    assert exc.value.code == "sites.slug_taken"
    assert deploy.names == []
    row = await _row(oid)
    assert (row["slug"], row["slug_pending"]) == ("acme-bakery", "acme-cakes")


@pytest.mark.asyncio
async def test_a_republish_without_a_pending_rename_stays_put(beanie_test_db, cf):
    oid = await _seed_live("pk-r", hosts=("acme.com",))
    cf.account.add("acme-bakery")
    deploy = _Deployer()

    await _publish("pk-r", deploy)

    assert deploy.names == ["acme-bakery"]
    assert cf.log == []
    assert (await _row(oid))["slug"] == "acme-bakery"


# ── the Cloudflare call ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_worker_route_puts_the_route_in_place():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True, "result": {"id": "r1"}})

    client = CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )
    await client.update_worker_route("r1", pattern="acme.com/*", script="acme-cakes")

    assert (seen["method"], seen["path"]) == ("PUT", "/client/v4/zones/zone_1/workers/routes/r1")
    assert b'"script":"acme-cakes"' in seen["body"].replace(b" ", b"")


# ── HTTP ─────────────────────────────────────────────────────────────────────


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str) -> None:
        self.id = OWNER
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id)]


def _build_app(workspace_id: str = WS) -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    fake_user = _FakeUser(workspace_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=OWNER,
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest.fixture
def fresh_check_limiter(monkeypatch):
    """The availability limiter is module-level and in-memory; give each test its own
    so the 31st check anywhere in the run is not the one that 429s."""
    from pocketpaw_ee.cloud._core import rate_limit

    from pocketpaw.security.rate_limiter import RateLimiter

    limiter = RateLimiter(rate=30.0 / 60.0, capacity=30)
    monkeypatch.setattr(rate_limit, "_slug_check_limiter", limiter)
    return limiter


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_http_slug_available_answers_the_dto(beanie_test_db, account, fresh_check_limiter):
    await _seed_live("pk-holder", slug="acme-cakes", workspace=OTHER_WS)

    async with _client(_build_app()) as c:
        res = await c.get("/api/v1/sites/slug-available", params={"slug": "Acme Cakes"})

    assert res.status_code == 200
    assert res.json() == {
        "available": False,
        "normalized": "acme-cakes",
        "reason": "taken",
        "suggestion": "acme-cakes-2",
    }


@pytest.mark.asyncio
async def test_http_slug_available_is_rate_limited_per_user(
    beanie_test_db, account, fresh_check_limiter
):
    async with _client(_build_app()) as c:
        codes = [
            (await c.get("/api/v1/sites/slug-available", params={"slug": "acme"})).status_code
            for _ in range(31)
        ]

    assert codes[:30] == [200] * 30
    assert codes[30] == 429


@pytest.mark.asyncio
async def test_http_rename_status_codes(beanie_test_db, account):
    await _seed_live("pk-holder", slug="acme-taken", workspace=OTHER_WS)
    oid = await _seed_live("pk-r")

    async with _client(_build_app()) as c:
        ok = await c.put(f"/api/v1/sites/{oid}/slug", json={"slug": "Acme Cakes"})
        taken = await c.put(f"/api/v1/sites/{oid}/slug", json={"slug": "acme-taken"})
        invalid = await c.put(f"/api/v1/sites/{oid}/slug", json={"slug": "-"})
        cancel = await c.delete(f"/api/v1/sites/{oid}/slug/pending")

    assert ok.status_code == 200
    assert (ok.json()["slug"], ok.json()["slug_pending"]) == ("acme-bakery", "acme-cakes")
    assert (taken.status_code, taken.json()["error"]["code"]) == (409, "sites.slug_taken")
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "sites.slug_invalid")
    assert (cancel.status_code, cancel.json()["slug_pending"]) == (200, None)


@pytest.mark.asyncio
async def test_http_rename_rate_limit_is_429(beanie_test_db, account):
    now = datetime.now(UTC)
    oid = await _seed_live("pk-r", changes=[now, now, now])

    async with _client(_build_app()) as c:
        res = await c.put(f"/api/v1/sites/{oid}/slug", json={"slug": "acme-cakes"})

    assert (res.status_code, res.json()["error"]["code"]) == (429, "sites.slug_rate_limited")


@pytest.mark.asyncio
async def test_http_rename_across_tenants_is_404(beanie_test_db, account):
    oid = await _seed_live("pk-foreign", workspace=OTHER_WS)

    async with _client(_build_app()) as c:
        put = await c.put(f"/api/v1/sites/{oid}/slug", json={"slug": "acme-cakes"})
        delete = await c.delete(f"/api/v1/sites/{oid}/slug/pending")

    assert (put.status_code, delete.status_code) == (404, 404)
    assert (await _row(oid))["slug_pending"] is None
