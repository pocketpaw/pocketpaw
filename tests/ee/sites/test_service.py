# tests/ee/sites/test_service.py — exercises the publish + domain orchestration
# with fakes injected for the generator + Cloudflare client so no Bun/workerd/CF
# is touched. Uses the shared ``beanie_test_db`` fixture (tests/ee/conftest.py)
# to init Beanie against an in-memory Mongo so the service can persist Site docs.
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
# Updated 2026-05-30 (security hardening, H3): added coverage that a malformed
# site_id on the authed paths raises NotFound (404) instead of leaking a raw
# bson InvalidId as an unhandled 500.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True

    async def create_custom_hostname(self, hostname):
        return CustomHostname(
            id="ch_1",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def get_hostname_status(self, hostname_id):
        return HostnameStatus.LIVE


@pytest.mark.asyncio
async def test_publish_generates_deploys_and_persists_site(beanie_test_db):
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    assert site.deployed is True
    # WfP script name == site id. ``script_name`` is the str form (model field
    # is typed str + used as the CF URL path segment); ``site.id`` is an
    # ObjectId, so compare against its str form.
    assert site.script_name == str(site.id)
    assert cf.put_calls == [site.script_name]
    assert site.signed_key  # a per-site signed key was minted


@pytest.mark.asyncio
async def test_add_domain_returns_one_cname_to_paste(beanie_test_db):
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    dom = await sites_service.add_domain(
        workspace_id="ws1",
        site_id=site.id,
        hostname="www.brightsmiledental.com",
        _cloudflare=cf,
    )
    assert dom.hostname == "www.brightsmiledental.com"
    assert dom.cname_target == "zone_1.cdn.cloudflare.net"
    assert dom.status == "pending"


@pytest.mark.asyncio
async def test_domain_status_polls_cloudflare(beanie_test_db):
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    await sites_service.add_domain(
        workspace_id="ws1",
        site_id=site.id,
        hostname="www.brightsmiledental.com",
        _cloudflare=cf,
    )
    status = await sites_service.domain_status(
        workspace_id="ws1",
        site_id=site.id,
        hostname="www.brightsmiledental.com",
        _cloudflare=cf,
    )
    assert status.status == "live"


@pytest.mark.asyncio
async def test_add_domain_malformed_site_id_raises_not_found(beanie_test_db):
    """H3: a malformed site_id is not a valid ObjectId. The cast must be guarded
    so the caller gets a 404 NotFound, not an unhandled bson InvalidId → 500."""
    cf = _FakeCF()
    with pytest.raises(NotFound):
        await sites_service.add_domain(
            workspace_id="ws1",
            site_id="not-a-valid-objectid",
            hostname="www.example.com",
            _cloudflare=cf,
        )


@pytest.mark.asyncio
async def test_domain_status_malformed_site_id_raises_not_found(beanie_test_db):
    """H3: the domain_status path also flows through _load — a malformed site_id
    must surface as NotFound, never InvalidId."""
    cf = _FakeCF()
    with pytest.raises(NotFound):
        await sites_service.domain_status(
            workspace_id="ws1",
            site_id="@@@not-an-objectid@@@",
            hostname="www.example.com",
            _cloudflare=cf,
        )
