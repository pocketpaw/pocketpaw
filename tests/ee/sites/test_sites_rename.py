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
#     the same pattern is a Cloudflare 10020), old Worker replaced by a redirect, doc
#     updated, old slug held; a failed re-point puts every route back, deletes the new
#     Worker and keeps ``slug_pending``; a failed deploy changes nothing; a redirect
#     that cannot deploy falls back to deleting the old Worker and never fails the
#     publish; a legacy ``paw-site-<id>`` rename holds nothing; a pending name that
#     appeared in the account is refused;
#   * the old address redirects (``slug_redirect``): a 308 to the same path and query
#     on the new host, only ever to the site's current address (chained renames
#     collapse, an own old name can be taken back), removed when the hold expires and
#     when the site is deleted, and the Worker never lets a request pick the host.
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
from pocketpaw_ee.cloud.models.site_redirect import SiteRedirect
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
        self.fail_put: set[str] = set()
        # Script name -> the source uploaded through the API (the redirect Workers).
        self.sources: dict[str, bytes] = {}
        self.uploads: list[dict] = []
        self.forced: list[str] = []

    async def update_worker_route(self, route_id, *, pattern, script):
        self.log.append(("route", route_id, script))
        if (route_id, script) in self.fail_route:
            raise RuntimeError(f"cloudflare refused route {route_id}")
        self.routes[route_id] = script

    async def delete_account_script(self, name, *, force=False):
        self.log.append(("delete", name))
        if force:
            self.forced.append(name)
        if name in self.fail_delete:
            raise RuntimeError(f"cloudflare refused to delete {name}")
        self.account.discard(name)

    async def put_worker(self, *, script_name, target, modules, main_module, bindings, **kw):
        self.log.append(("put", script_name, target))
        self.uploads.append(
            {
                "name": script_name,
                "target": target,
                "main_module": main_module,
                "bindings": bindings,
                "modules": [m.name for m in modules],
                **kw,
            }
        )
        if script_name in self.fail_put:
            raise RuntimeError(f"cloudflare refused to upload {script_name}")
        [module] = [m for m in modules if m.name == main_module]
        self.sources[script_name] = module.content
        self.account.add(script_name)
        return True

    async def clear_schedules(self, name):
        self.log.append(("schedules", name))

    async def enable_workers_dev(self, name):
        self.log.append(("subdomain", name))

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


async def _redirects() -> list[dict]:
    return await SiteRedirect.get_pymongo_collection().find({}).sort("name", 1).to_list(None)


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
    # ...and only then was the old Worker replaced by a redirect to the new address.
    assert cf.log[-3:] == [
        ("put", "acme-bakery", "account"),
        ("schedules", "acme-bakery"),
        ("subdomain", "acme-bakery"),
    ]
    assert b'"https://acme-cakes.acct.workers.dev"' in cf.sources["acme-bakery"]
    assert cf.deleted() == []
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
async def test_a_failed_redirect_falls_back_to_deleting_the_old_worker(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.account.add("acme-bakery")
    cf.fail_put.add("acme-bakery")

    await _publish("pk-r", _Deployer())

    assert cf.deleted() == ["acme-bakery"]
    assert await _redirects() == []
    assert (await _row(oid))["slug"] == "acme-cakes"


@pytest.mark.asyncio
async def test_a_failed_redirect_and_delete_do_not_fail_the_publish(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.fail_put.add("acme-bakery")
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
    # The legacy address was a live link too, so it redirects; it is just never held.
    assert [r["name"] for r in await _redirects()] == [f"paw-site-{oid}"]
    assert cf.deleted() == []
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


# ── the old address redirects ────────────────────────────────────────────────


async def _rename(oid, pocket_id: str, raw: str) -> None:
    await _reserve(oid, raw)
    await _publish(pocket_id, _Deployer())


@pytest.mark.asyncio
async def test_the_old_address_308s_to_the_new_one_for_the_hold(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.account.add("acme-bakery")

    await _publish("pk-r", _Deployer())

    [redirect] = await _redirects()
    assert (redirect["name"], redirect["site_id"], redirect["workspace_id"]) == (
        "acme-bakery",
        str(oid),
        WS,
    )
    assert redirect["target_url"] == "https://acme-cakes.acct.workers.dev"
    # Lives exactly as long as the hold on the old name.
    [hold] = await _holds()
    assert redirect["expires_at"] - redirect["created_at"] == timedelta(days=30)
    assert abs(redirect["expires_at"] - hold["hold_until"]) < timedelta(seconds=5)
    # The old name still answers, from the redirect Worker.
    assert "acme-bakery" in cf.account
    src = cf.sources["acme-bakery"].decode()
    assert "status: 308" in src
    assert 'const TARGET = "https://acme-cakes.acct.workers.dev";' in src


_RUN_WORKER = r"""
const src = require("fs").readFileSync(process.argv[2], "utf8");
const mod = { exports: {} };
new Function("module", src.replace("export default", "module.exports ="))(mod);
const out = [];
for (const url of process.argv.slice(3)) {
  const res = mod.exports.fetch(new Request(url));
  out.push([res.status, res.headers.get("location"), res.headers.get("cache-control")]);
}
console.log(JSON.stringify(out));
"""


def test_the_redirect_worker_keeps_path_and_query_and_never_changes_host(tmp_path):
    import json
    import shutil
    import subprocess
    from urllib.parse import urlsplit

    from pocketpaw_ee.sites.slug_redirect import redirect_worker_source

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    worker = tmp_path / "redirect.js"
    worker.write_bytes(redirect_worker_source("https://acme-cakes.acct.workers.dev"))
    runner = tmp_path / "run.js"
    runner.write_text(_RUN_WORKER)
    urls = [
        "https://acme-bakery.acct.workers.dev/menu/today?table=4&x=%2F",
        "https://acme-bakery.acct.workers.dev/",
        "https://acme-bakery.acct.workers.dev//evil.example/steal?to=https://evil.example",
        "https://acme-bakery.acct.workers.dev/@evil.example",
        "https://acme-bakery.acct.workers.dev/a%0d%0aSet-Cookie:%20x=1?b=%0d%0a",
        "https://acme-bakery.acct.workers.dev/\\evil.example/x",
        "https://acme-bakery.acct.workers.dev/%2F%2Fevil.example/x",
        "https://acme-bakery.acct.workers.dev/caf%C3%A9/%E0%A4%AE%E0%A5%87%E0%A4%A8%E0%A5%82",
    ]
    done = subprocess.run(
        [node, str(runner), str(worker), *urls], capture_output=True, text=True, check=True
    )
    got = json.loads(done.stdout)

    # 308 keeps the method (a form POST to the old address stays a POST); the short
    # private cache lets a rename-back take effect within minutes.
    assert got[0] == [
        308,
        "https://acme-cakes.acct.workers.dev/menu/today?table=4&x=%2F",
        "private, max-age=300",
    ]
    assert got[1][1] == "https://acme-cakes.acct.workers.dev/"
    for _status, location, _cache in got:
        assert urlsplit(location).hostname == "acme-cakes.acct.workers.dev", location
        assert "\r" not in location and "\n" not in location, location
    # Encoded slashes and unicode stay encoded path bytes on the target host.
    assert got[6][1] == "https://acme-cakes.acct.workers.dev/%2F%2Fevil.example/x"
    assert got[7][1].endswith("/caf%C3%A9/%E0%A4%AE%E0%A5%87%E0%A4%A8%E0%A5%82")


@pytest.mark.parametrize(
    "url",
    [
        "http://acme-cakes.acct.workers.dev",
        "javascript:alert(1)",
        "https://user:pw@acme-cakes.acct.workers.dev",
        "https://evil.example@acme-cakes.acct.workers.dev",
        "https://acme-cakes.acct.workers.dev:8443",
        "https://",
        "https://acme cakes.workers.dev",
        "https://acme-cakes",
        "//acme-cakes.acct.workers.dev",
        "",
    ],
)
def test_a_redirect_only_ever_targets_a_plain_https_host(url):
    from pocketpaw_ee.sites.slug_redirect import redirect_origin, redirect_worker_source

    with pytest.raises(ValueError):
        redirect_origin(url)
    with pytest.raises(ValueError):
        redirect_worker_source(url)


def test_a_redirect_targets_the_origin_of_the_sites_address():
    from pocketpaw_ee.sites.slug_redirect import redirect_origin

    assert redirect_origin("https://Acme-Cakes.acct.workers.dev/some/page?q=1") == (
        "https://acme-cakes.acct.workers.dev"
    )


@pytest.mark.asyncio
async def test_a_bad_site_address_deletes_the_old_worker_instead(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.account.add("acme-bakery")

    class _HttpDeployer(_Deployer):
        async def __call__(self, site_id, project_dir, *, worker_name=None, **_):
            self.names.append(worker_name)
            return f"http://{worker_name}.acct.workers.dev"

    await _publish("pk-r", _HttpDeployer())

    assert "acme-bakery" not in cf.sources
    assert cf.deleted() == ["acme-bakery"]
    assert await _redirects() == []
    assert (await _row(oid))["slug"] == "acme-cakes"


@pytest.mark.asyncio
async def test_a_chained_rename_sends_every_old_address_straight_to_the_newest(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")

    await _publish("pk-r", _Deployer())
    await _rename(oid, "pk-r", "acme-pies")

    redirects = await _redirects()
    assert [(r["name"], r["target_url"]) for r in redirects] == [
        ("acme-bakery", "https://acme-pies.acct.workers.dev"),
        ("acme-cakes", "https://acme-pies.acct.workers.dev"),
    ]
    assert b'"https://acme-pies.acct.workers.dev"' in cf.sources["acme-bakery"]
    assert b'"https://acme-pies.acct.workers.dev"' in cf.sources["acme-cakes"]
    assert (await _row(oid))["slug"] == "acme-pies"


@pytest.mark.asyncio
async def test_a_site_can_take_its_own_old_address_back(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    cf.account.add("acme-bakery")
    await _publish("pk-r", _Deployer())

    assert (await _available("acme-bakery", site_id=oid)).available
    await _reserve(oid, "acme-bakery")
    deploy = _Deployer()
    await _publish("pk-r", deploy)

    assert deploy.names == ["acme-bakery"]
    assert (await _row(oid))["slug"] == "acme-bakery"
    # acme-bakery is the site again; only acme-cakes redirects, to it.
    assert [(r["name"], r["target_url"]) for r in await _redirects()] == [
        ("acme-cakes", "https://acme-bakery.acct.workers.dev"),
    ]


@pytest.mark.asyncio
async def test_another_site_cannot_take_a_redirecting_address(beanie_test_db, cf):
    await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    other = await _seed_live("pk-other", slug="acme-other")

    mine = await _available("acme-bakery", site_id=other)
    elsewhere = await _available("acme-bakery", workspace=OTHER_WS)

    assert (mine.available, mine.reason) == (False, "taken")
    assert not elsewhere.available


@pytest.mark.asyncio
async def test_an_expired_redirect_is_removed_and_the_address_freed(beanie_test_db, cf):
    from pocketpaw_ee.sites.slug_redirect import sweep_slug_redirects

    await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    now = datetime.now(UTC)

    early = await sweep_slug_redirects(cf=cf, now=now + timedelta(days=29))
    assert early == {"expired": 0, "removed": 0}
    assert "acme-bakery" in cf.account

    late = await sweep_slug_redirects(cf=cf, now=now + timedelta(days=31))
    assert late == {"expired": 1, "removed": 1}
    assert cf.deleted() == ["acme-bakery"]
    assert await _redirects() == []
    assert "acme-bakery" not in cf.account


@pytest.mark.asyncio
async def test_an_expired_redirect_that_cannot_be_deleted_waits_for_the_next_tick(
    beanie_test_db, cf
):
    from pocketpaw_ee.sites.slug_redirect import sweep_slug_redirects

    await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    cf.fail_delete.add("acme-bakery")

    out = await sweep_slug_redirects(cf=cf, now=datetime.now(UTC) + timedelta(days=31))

    assert out == {"expired": 1, "removed": 0}
    assert [r["name"] for r in await _redirects()] == ["acme-bakery"]


def test_the_redirect_sweep_runs_on_the_shared_heartbeat():
    from pocketpaw_ee import extensions
    from pocketpaw_ee.sites.slug_redirect import sweep_slug_redirects

    cluster, _per_host = extensions._sweeps()
    assert sweep_slug_redirects in cluster


@pytest.mark.asyncio
async def test_deleting_the_site_removes_its_redirects(beanie_test_db, cf):
    from pocketpaw_ee.sites import delete_cascade
    from pocketpaw_ee.sites.delete_job import _DeleteDeps

    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    await _rename(oid, "pk-r", "acme-pies")
    site = await _SiteDoc.get(oid)

    await delete_cascade._delete_script(site=site, deps=_DeleteDeps(cloudflare=cf, assets=None))

    assert sorted(cf.deleted()) == ["acme-bakery", "acme-cakes", "acme-pies"]
    assert await _redirects() == []


# ── redirect guards (security review) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_redirect_upload_is_a_bare_account_worker(beanie_test_db, cf):
    await _seed_live("pk-r", pending="acme-cakes")

    await _publish("pk-r", _Deployer())

    [upload] = cf.uploads
    assert upload["name"] == "acme-bakery"
    assert upload["target"] == "account"
    assert upload["bindings"] == []
    assert upload["modules"] == [upload["main_module"]]
    assert upload["compatibility_date"]
    # Any cron the old site had is cleared, and workers.dev serves it (previews off
    # is enable_workers_dev's own body).
    assert ("schedules", "acme-bakery") in cf.log
    assert ("subdomain", "acme-bakery") in cf.log


@pytest.mark.asyncio
async def test_a_redirect_never_lands_on_a_name_a_site_serves(beanie_test_db, cf):
    from pocketpaw_ee.sites import slug_redirect

    await _seed_live("pk-other", slug="acme-taken")

    done = await slug_redirect.deploy_redirect(
        cf,
        name="acme-taken",
        site_id="site-x",
        workspace_id=WS,
        url="https://acme-cakes.acct.workers.dev",
        days=30,
    )

    assert done is False
    assert cf.uploads == []
    assert await _redirects() == []


@pytest.mark.asyncio
async def test_a_retarget_drops_a_redirect_whose_name_a_site_now_claims(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    # Another site now has acme-bakery pending (the row should not exist any more,
    # but if it does the redirect must never be written over that site's name).
    await _seed_live("pk-other", slug="acme-other", pending="acme-bakery")
    before = cf.sources["acme-bakery"]

    await _rename(oid, "pk-r", "acme-pies")

    assert cf.sources["acme-bakery"] == before
    assert [r["name"] for r in await _redirects()] == ["acme-cakes"]


@pytest.mark.asyncio
async def test_the_sweep_never_deletes_a_name_a_site_has_pending(beanie_test_db, cf):
    from pocketpaw_ee.sites.slug_redirect import sweep_slug_redirects

    await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    await _seed_live("pk-other", slug="acme-other", pending="acme-bakery")

    out = await sweep_slug_redirects(cf=cf, now=datetime.now(UTC) + timedelta(days=31))

    assert out == {"expired": 1, "removed": 1}
    assert cf.deleted() == []
    assert await _redirects() == []


@pytest.mark.asyncio
async def test_remove_site_redirects_skips_a_name_another_site_serves(beanie_test_db, cf):
    from pocketpaw_ee.sites.slug_redirect import remove_site_redirects

    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    await _seed_live("pk-other", slug="acme-other")
    await _SiteDoc.get_pymongo_collection().update_one(
        {"pocket_id": "pk-other"}, {"$set": {"slug": "acme-bakery"}}
    )

    await remove_site_redirects(cf, site_id=str(oid))

    assert cf.deleted() == []
    assert await _redirects() == []


@pytest.mark.asyncio
async def test_taking_an_old_address_back_drops_its_row_before_the_deploy(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    await _reserve(oid, "acme-bakery")
    seen_at_deploy: list[list[str]] = []

    class _Watching(_Deployer):
        async def __call__(self, site_id, project_dir, *, worker_name=None, **kw):
            seen_at_deploy.append([r["name"] for r in await _redirects()])
            return await super().__call__(site_id, project_dir, worker_name=worker_name, **kw)

    await _publish("pk-r", _Watching())

    assert seen_at_deploy == [[]]
    assert [(r["name"], r["target_url"]) for r in await _redirects()] == [
        ("acme-cakes", "https://acme-bakery.acct.workers.dev"),
    ]


@pytest.mark.asyncio
async def test_an_expired_own_redirect_is_not_a_take_back(beanie_test_db, cf):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    await SiteRedirect.get_pymongo_collection().update_many(
        {}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}}
    )

    out = await _available("acme-bakery", site_id=oid)

    assert (out.available, out.reason) == (False, "taken")


@pytest.mark.asyncio
async def test_an_own_redirect_row_does_not_override_another_sites_worker(beanie_test_db, cf):
    oid = await _seed_live("pk-r", slug="acme-cakes")
    now = datetime.now(UTC)
    await SiteRedirect(
        name="acme-bakery",
        site_id=str(oid),
        workspace_id=WS,
        target_url="https://acme-cakes.acct.workers.dev",
        created_at=now,
        expires_at=now + timedelta(days=30),
    ).insert()
    other = await _seed_live("pk-other", slug="acme-other")
    await _SiteDoc.get_pymongo_collection().update_one(
        {"_id": other}, {"$set": {"worker_name": "acme-bakery"}}
    )

    out = await _available("acme-bakery", site_id=oid)

    assert (out.available, out.reason) == (False, "taken")


@pytest.mark.asyncio
async def test_a_durable_object_site_deletes_its_old_worker_instead(beanie_test_db, cf, caplog):
    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _SiteDoc.get_pymongo_collection().update_one(
        {"_id": oid}, {"$set": {"do_classes": ["Counter"]}}
    )
    cf.account.add("acme-bakery")

    with caplog.at_level("ERROR"):
        await _publish("pk-r", _Deployer())

    assert cf.uploads == []
    assert cf.forced == ["acme-bakery"]
    assert await _redirects() == []
    assert "old address" in caplog.text and "acme-bakery" in caplog.text


@pytest.mark.asyncio
async def test_a_rename_only_forgets_its_own_rows_for_the_new_name(beanie_test_db, cf):
    await _seed_live("pk-r", pending="acme-cakes")
    now = datetime.now(UTC)
    await SiteRedirect(
        name="acme-cakes",
        site_id="someone-else",
        workspace_id=OTHER_WS,
        target_url="https://elsewhere.acct.workers.dev",
        created_at=now,
        expires_at=now + timedelta(days=30),
    ).insert()

    await _publish("pk-r", _Deployer())

    assert ("acme-cakes", "someone-else") in [(r["name"], r["site_id"]) for r in await _redirects()]


@pytest.mark.asyncio
async def test_deleting_mid_chain_after_a_failed_retarget_removes_everything(beanie_test_db, cf):
    from pocketpaw_ee.sites import delete_cascade
    from pocketpaw_ee.sites.delete_job import _DeleteDeps

    oid = await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    cf.fail_put.add("acme-bakery")
    await _rename(oid, "pk-r", "acme-pies")
    # acme-bakery could not be re-pointed: it still sends people to acme-cakes,
    # which now redirects to acme-pies (one hop longer, never broken).
    targets = {r["name"]: r["target_url"] for r in await _redirects()}
    assert targets == {
        "acme-bakery": "https://acme-cakes.acct.workers.dev",
        "acme-cakes": "https://acme-pies.acct.workers.dev",
    }

    site = await _SiteDoc.get(oid)
    await delete_cascade._delete_script(site=site, deps=_DeleteDeps(cloudflare=cf, assets=None))

    assert sorted(cf.deleted()) == ["acme-bakery", "acme-cakes", "acme-pies"]
    assert await _redirects() == []


@pytest.mark.asyncio
async def test_a_sweep_without_cloudflare_credentials_keeps_its_rows(
    beanie_test_db, cf, monkeypatch
):
    from pocketpaw_ee.sites.slug_redirect import sweep_slug_redirects

    await _seed_live("pk-r", pending="acme-cakes")
    await _publish("pk-r", _Deployer())
    monkeypatch.setattr(sites_service, "_cf_account_client", lambda: None)

    out = await sweep_slug_redirects(now=datetime.now(UTC) + timedelta(days=31))

    assert out == {"expired": 1, "removed": 0}
    assert [r["name"] for r in await _redirects()] == ["acme-bakery"]


@pytest.mark.asyncio
async def test_clear_schedules_puts_an_empty_cron_list():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True, "result": {"schedules": []}})

    client = CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )
    await client.clear_schedules("acme-bakery")

    assert (seen["method"], seen["path"]) == (
        "PUT",
        "/client/v4/accounts/acct_1/workers/scripts/acme-bakery/schedules",
    )
    assert seen["body"].replace(b" ", b"") == b"[]"


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
