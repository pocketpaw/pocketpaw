# tests/ee/sites/test_sites_worker_name.py
# Created: 2026-09-23 (VS-1, feat/sites-worker-name-decouple) — the Worker a site
# deploys under is a stored field, not a derivation from the site id.
#
# ``Site.worker_name`` exists so a later slice can name a Worker after a user-chosen
# slug. Nothing sets it yet, so the contract pinned here has two halves:
#
#   1. LEGACY ROWS ARE UNTOUCHED. A row with ``worker_name=None`` resolves to
#      ``paw-site-<id>``, byte for byte what ``worker_name(site_id)`` returned before.
#   2. A STORED NAME WINS EVERYWHERE THAT MEANS "THIS SITE'S WORKER". Deploy, the URL
#      fallback, custom-domain routes (first add and the repair re-add) and the delete
#      cascade must all name the same script. Cloudflare rejects a route naming a script
#      that does not exist, and a delete aimed at the wrong name 404s, which the cascade
#      counts as success — so a seam that kept deriving the name would fail silently.
#
# The WfP lane is deliberately NOT covered by the stored name: a dispatch-namespace
# script is named by the bare site id, and one test here pins that it stays so.
#
# Updated 2026-09-23 (VS-2): a first ``workers`` publish now claims a name-based
# address, so the publish test no longer expects ``paw-site-<id>`` on the first deploy.
#
# MUTATION THAT BREAKS THIS FILE: reverting ``workers_deploy.site_worker_name`` to
# ``worker_name(str(doc.id))``.
from __future__ import annotations

import json
from pathlib import Path

import pytest
from bson import ObjectId
from pocketpaw_ee.cloud.models.site import SiteDomain as _SiteDomainDoc
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites import workers_deploy
from pocketpaw_ee.sites.delete_cascade import run_cascade
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus

_OID = "507f1f77bcf86cd799439011"


# ── the accessor ─────────────────────────────────────────────────────────────


class _Row:
    def __init__(self, id, **kw):
        self.id = id
        self.__dict__.update(kw)


def test_a_legacy_row_resolves_to_the_derived_name_byte_for_byte():
    """``worker_name=None`` is every row that exists today. It must name the Worker it
    was actually deployed under, or every live site loses its routes and its delete."""
    row = _Row(ObjectId(_OID), worker_name=None)

    assert workers_deploy.site_worker_name(row) == f"paw-site-{_OID}"
    assert workers_deploy.site_worker_name(row) == workers_deploy.worker_name(_OID)


def test_a_row_without_the_field_takes_the_legacy_answer():
    """Duck-typed rows (the cascade's own test doubles, a projection) may not carry
    the attribute at all."""
    assert workers_deploy.site_worker_name(_Row(_OID)) == f"paw-site-{_OID}"


def test_a_stored_name_wins():
    assert workers_deploy.site_worker_name(_Row(ObjectId(_OID), worker_name="acme")) == "acme"


# ── deploy_workers: the wrangler seam ────────────────────────────────────────


class _FakeProc:
    def __init__(self, stdout: bytes = b""):
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


def _build_project(tmp_path: Path) -> str:
    out = tmp_path / ".svelte-kit" / "cloudflare"
    out.mkdir(parents=True)
    (out / "_worker.js").write_text("export default {}")
    (out / "index.html").write_text("<h1>hi</h1>")
    return str(tmp_path)


@pytest.mark.asyncio
async def test_deploy_uses_the_stored_name_for_config_and_url(tmp_path, monkeypatch):
    """The ``wrangler.jsonc`` ``name`` is what Cloudflare calls the Worker, and the URL
    fallback rebuilds the workers.dev address from it. The D1 is a DATABASE, created and
    migrated as ``paw-site-<id>`` — renaming the Worker must not rename what it binds."""

    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        return _FakeProc(b"deployed, no url printed\n")

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "acct")
    project = _build_project(tmp_path)

    url = await workers_deploy.deploy_workers(
        _OID,
        project,
        d1_database_id="d1-7",
        analytics_entitled=False,
        worker_name=workers_deploy.site_worker_name(_Row(ObjectId(_OID), worker_name="acme")),
    )

    assert url == "https://acme.acct.workers.dev"
    config = json.loads((tmp_path / "wrangler.jsonc").read_text())
    assert config["name"] == "acme"
    assert config["d1_databases"][0]["database_name"] == f"paw-site-{_OID}"


@pytest.mark.asyncio
async def test_deploy_without_a_stored_name_is_unchanged(tmp_path, monkeypatch):
    async def _fake_exec(*argv, cwd=None, env=None, stdout=None, stderr=None):
        return _FakeProc(b"deployed, no url printed\n")

    monkeypatch.setattr(workers_deploy.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "acct")
    project = _build_project(tmp_path)

    url = await workers_deploy.deploy_workers(
        _OID, project, d1_database_id="d1-7", analytics_entitled=False
    )

    assert url == f"https://paw-site-{_OID}.acct.workers.dev"
    config = json.loads((tmp_path / "wrangler.jsonc").read_text())
    assert config["name"] == f"paw-site-{_OID}"
    assert config["d1_databases"][0]["database_name"] == f"paw-site-{_OID}"


# ── the publish path hands the deployer the row's name ───────────────────────


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def _recording_deployer(seen: list):
    async def _deploy(site_id: str, project_dir: str, *, worker_name=None, **_: object):
        seen.append(worker_name)
        return f"https://{worker_name}.acct.workers.dev"

    return _deploy


async def _publish(pocket_id: str, deployer):
    return await sites_service.publish(
        workspace_id="ws-worker-name",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Acme",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=deployer,
    )


@pytest.mark.asyncio
async def test_publish_deploys_under_the_rows_stored_name(beanie_test_db, monkeypatch):
    """Once the row stores a name, the re-publish deploys under it — the same Worker,
    not a second. (Since VS-2 a first publish claims a name-based address itself; that
    lane is pinned in test_sites_first_publish_slug.py. Here the stored name is changed
    by hand to prove the deploy reads the row, not the claim.)"""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    seen: list = []

    site = await _publish("pk-acme", _recording_deployer(seen))
    assert seen == ["acme"]

    site.worker_name = "acme-by-hand"
    await site.save()
    site = await _publish("pk-acme", _recording_deployer(seen))

    assert seen[-1] == "acme-by-hand"
    assert site.url == "https://acme-by-hand.acct.workers.dev"


@pytest.mark.asyncio
async def test_provision_deploy_passes_the_name_through(monkeypatch):
    seen: dict = {}

    async def _fake_deploy(site_id, project_dir, *, worker_name=None, **_: object):
        seen["worker_name"] = worker_name
        return "https://acme.acct.workers.dev"

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.setattr(workers_deploy, "deploy_workers", _fake_deploy)

    url, target = await sites_service.provision_deploy(
        site_id=_OID, project_dir="/tmp/x", bundle=b"", d1_database_id="d1", worker_name="acme"
    )

    assert (url, target) == ("https://acme.acct.workers.dev", "workers")
    assert seen["worker_name"] == "acme"


# ── custom-domain routes ─────────────────────────────────────────────────────


class _FakeCF:
    def __init__(self):
        self.calls: list[tuple] = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True

    async def create_custom_hostname(self, hostname, *, features=None):
        self.calls.append(("create_hostname", hostname))
        return CustomHostname(
            id="ch_1",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="sites.pawzone.test",
        )

    async def create_worker_route(self, *, pattern, script):
        self.calls.append(("create_route", pattern, script))
        return "route_1"


async def _make_named_site(pocket_id: str, *, worker_name: str | None) -> str:
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Acme",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda _d: b"x",
    )
    site.deployed = True
    site.deploy_target = "workers"
    site.worker_name = worker_name
    await site.save()
    return str(site.id)


@pytest.mark.asyncio
async def test_add_domain_routes_to_the_stored_worker(beanie_test_db):
    site_id = await _make_named_site("pk-route-acme", worker_name="acme")
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.acme.test", _cloudflare=cf
    )

    assert cf.calls == [
        ("create_hostname", "www.acme.test"),
        ("create_route", "www.acme.test/*", "acme"),
    ]


@pytest.mark.asyncio
async def test_re_adding_a_routeless_domain_repairs_it_against_the_stored_worker(beanie_test_db):
    site_id = await _make_named_site("pk-repair-acme", worker_name="acme")
    site = await sites_service._load("ws1", site_id)
    site.domains.append(
        _SiteDomainDoc(
            hostname="www.acme.test",
            cf_hostname_id="ch_legacy",
            cname_target="sites.pawzone.test",
            status="live",
        )
    )
    await site.save()
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.acme.test", _cloudflare=cf
    )

    assert cf.calls == [("create_route", "www.acme.test/*", "acme")]


@pytest.mark.asyncio
async def test_a_legacy_site_still_routes_to_the_derived_worker(beanie_test_db):
    site_id = await _make_named_site("pk-route-legacy", worker_name=None)
    cf = _FakeCF()

    await sites_service.add_domain(
        workspace_id="ws1", site_id=site_id, hostname="www.legacy.test", _cloudflare=cf
    )

    assert cf.calls[-1] == ("create_route", "www.legacy.test/*", f"paw-site-{site_id}")


# ── the delete cascade ───────────────────────────────────────────────────────


class _CascadeSite:
    def __init__(self, **kw):
        self.id = _OID
        self.workspace = "w1"
        self.pocket_id = "pk1"
        self.script_name = _OID  # the site id, as every publish writes it
        self.deploy_target = "workers"
        self.d1_database_id = ""
        self.signed_key = "key"
        self.revoked = False
        self.subscription_status = "none"
        self.renewal_date = None
        self.billing_rail = "credits"
        self.domains = []
        self.delete_ledger: dict[str, str] = {}
        self.__dict__.update(kw)


class _CascadeCF:
    def __init__(self):
        self.deleted: list[tuple[str, str]] = []

    async def delete_worker(self, name):
        self.deleted.append(("delete_worker", name))

    async def delete_account_script(self, name):
        self.deleted.append(("delete_account_script", name))

    async def delete_database(self, _id):  # pragma: no cover - no D1 on these rows
        pass


class _CascadeDeps:
    def __init__(self):
        self.cloudflare = _CascadeCF()
        self.assets = None

    async def purge_records(self, *, workspace_id, site_id):
        pass


async def _no_save(_site):
    pass


@pytest.mark.asyncio
async def test_the_cascade_deletes_the_stored_worker():
    deps = _CascadeDeps()

    await run_cascade(site=_CascadeSite(worker_name="acme"), deps=deps, save=_no_save)

    assert deps.cloudflare.deleted == [("delete_account_script", "acme")]


@pytest.mark.asyncio
async def test_the_cascade_deletes_a_legacy_sites_derived_worker():
    """Not the site id: a ``workers`` deploy never created a script by that name, so
    deleting it 404s — success here — and the real Worker keeps serving."""
    deps = _CascadeDeps()

    await run_cascade(site=_CascadeSite(), deps=deps, save=_no_save)

    assert deps.cloudflare.deleted == [("delete_account_script", f"paw-site-{_OID}")]


@pytest.mark.asyncio
async def test_the_wfp_cascade_still_deletes_by_site_id():
    """A dispatch-namespace script IS named by the site id; a stored Worker name must
    not leak into that lane."""
    deps = _CascadeDeps()

    await run_cascade(
        site=_CascadeSite(deploy_target="wfp", worker_name="acme"), deps=deps, save=_no_save
    )

    assert deps.cloudflare.deleted == [("delete_worker", _OID)]
