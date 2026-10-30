# tests/ee/sites/test_reserve.py — reproduce-first coverage for re-serving
# locally-deployed Paw Sites after a backend restart.
#
# The bug: site files persist under sites_home()/<site_id>/ across restarts, but
# the local static server is only started during PUBLISH and binds an ephemeral
# OS-assigned port. After a restart no server is running, so every stored Site
# ``url`` (e.g. http://127.0.0.1:56308/<id>/) is dead. reserve_local_sites()
# (re)starts the shared server and rewrites each deployed site's url to the live
# base, so prior sites become openable again.
#
# Created 2026-06-17 (feat/sites-local-reserve): tests for reserve_local_sites()
# (service) + POST /sites/reserve (router). Uses PAW_SITES_LOCAL=1 +
# PAW_SITES_LOCAL_DIR=<tmp> so the local-mode branch is selected and no real
# ~/.pocketpaw home is touched. The local_server singleton is reset per test so
# each run gets a fresh ephemeral port (mirrors a fresh process / restart).

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from pocketpaw_ee.sites import local_server
from pocketpaw_ee.sites import service as sites_service


@pytest.fixture(autouse=True)
def _local_sites_env(tmp_path, monkeypatch):
    """Select local-deploy mode and root the sites home + server at a temp dir.

    Also resets the local_server singleton before AND after the test so a server
    bound by a prior test (its own ephemeral port) never leaks into this one —
    the same reset a fresh backend process would see on restart.
    """
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(tmp_path / "sites"))
    monkeypatch.delenv("PAW_SITES_LOCAL_PORT", raising=False)
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    _shutdown_local_server()
    yield
    _shutdown_local_server()


def _shutdown_local_server() -> None:
    server = local_server._server
    if server is not None:
        server.shutdown()
        server.server_close()
    local_server._server = None


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _publish_local_site(workspace_id: str, pocket_id: str, name: str):
    """Publish a site through the LOCAL branch and persist real files under
    sites_home()/<site_id>/ so reserve_local_sites can find them. The local
    deploy is faked to just create the per-site dir (no Bun build)."""

    def fake_local_deploy(site_id: str, project_dir: str) -> str:
        # Create the persisted per-site dir the reconcile step looks for.
        dest = local_server.sites_home() / site_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        return local_server.local_url_for(site_id)

    return await sites_service.publish(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name=name,
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused",
        _local_deploy=fake_local_deploy,
    )


@pytest.mark.asyncio
async def test_reserve_restarts_server_and_rewrites_stale_url(beanie_test_db):
    """A site published in a prior 'process' has its files on disk but its stored
    url points at a dead ephemeral port (the old server is gone). After a restart
    (server singleton reset) reserve_local_sites() starts a fresh server and
    rewrites the url to the live base, so the site is openable again."""
    site = await _publish_local_site("ws1", "pk1", "Site One")
    original_url = site.url

    # Simulate the restart: tear down the server that publish started. The Site
    # row + its files survive (that's the whole point of the bug).
    _shutdown_local_server()

    count = await sites_service.reserve_local_sites()
    assert count == 1

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    fresh = await _SiteDoc.get(site.id)
    # The url now ends with /<site_id>/ and points at the fresh live base.
    assert fresh.url.endswith(f"/{site.id}/")
    live_base = local_server.ensure_server()
    assert fresh.url == f"{live_base}/{site.id}/"
    # The host:port matches the live server, not the dead one.
    live = urlparse(live_base)
    got = urlparse(fresh.url)
    assert (got.hostname, got.port) == (live.hostname, live.port)
    # And, concretely, the stale port was replaced (publish's server is gone).
    assert urlparse(original_url).port != got.port


@pytest.mark.asyncio
async def test_reserve_skips_sites_with_no_persisted_dir(beanie_test_db):
    """A deployed Site row whose files are NOT on disk (e.g. the dir was pruned)
    is skipped — reserve_local_sites only reconciles sites it can actually serve,
    and does not count or rewrite the orphan."""
    served = await _publish_local_site("ws1", "pk_served", "Served")
    orphan = await _publish_local_site("ws1", "pk_orphan", "Orphan")

    # Remove the orphan's persisted dir so it has no files to serve.
    import shutil

    shutil.rmtree(local_server.sites_home() / str(orphan.id))
    orphan_url_before = orphan.url

    _shutdown_local_server()
    count = await sites_service.reserve_local_sites()
    assert count == 1  # only the served site was reconciled

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    fresh_served = await _SiteDoc.get(served.id)
    fresh_orphan = await _SiteDoc.get(orphan.id)
    assert fresh_served.url.endswith(f"/{served.id}/")
    # The orphan was left untouched.
    assert fresh_orphan.url == orphan_url_before


@pytest.mark.asyncio
async def test_reserve_is_tenant_scoped(beanie_test_db):
    """reserve_local_sites(workspace_id) only touches that workspace's sites; an
    unscoped call (workspace_id=None) reconciles every workspace's sites."""
    site_a = await _publish_local_site("ws_a", "pk_a", "A")
    site_b = await _publish_local_site("ws_b", "pk_b", "B")
    url_b_before = site_b.url

    _shutdown_local_server()
    count = await sites_service.reserve_local_sites("ws_a")
    assert count == 1

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    fresh_a = await _SiteDoc.get(site_a.id)
    fresh_b = await _SiteDoc.get(site_b.id)
    live_base = local_server.ensure_server()
    assert fresh_a.url == f"{live_base}/{site_a.id}/"
    # ws_b was NOT in scope — its url is unchanged (still the stale one).
    assert fresh_b.url == url_b_before


@pytest.mark.asyncio
async def test_reserve_unscoped_reconciles_all_workspaces(beanie_test_db):
    """The boot hook calls reserve_local_sites() with no workspace so every prior
    local site across all tenants is re-served on a restart."""
    site_a = await _publish_local_site("ws_a", "pk_a", "A")
    site_b = await _publish_local_site("ws_b", "pk_b", "B")

    _shutdown_local_server()
    count = await sites_service.reserve_local_sites()
    assert count == 2

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    live_base = local_server.ensure_server()
    fresh_a = await _SiteDoc.get(site_a.id)
    fresh_b = await _SiteDoc.get(site_b.id)
    assert fresh_a.url == f"{live_base}/{site_a.id}/"
    assert fresh_b.url == f"{live_base}/{site_b.id}/"


@pytest.mark.asyncio
async def test_reserve_noop_when_not_local_mode(beanie_test_db, monkeypatch):
    """With real Cloudflare creds present (PAW_SITES_LOCAL unset), local mode is
    off and reserve_local_sites is a no-op: it never starts a server and never
    rewrites a url. Mirrors the existing _is_local()/_local_mode() gate."""
    # Build a local site first (while local mode is on, via the autouse fixture).
    site = await _publish_local_site("ws1", "pk1", "Site One")
    url_before = site.url
    _shutdown_local_server()

    # Now flip to a configured (CF) environment.
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_123")

    count = await sites_service.reserve_local_sites()
    assert count == 0
    assert local_server._server is None  # no server started

    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    fresh = await _SiteDoc.get(site.id)
    assert fresh.url == url_before  # untouched
