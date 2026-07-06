# tests/ee/sites/test_service.py — exercises the publish + domain orchestration
# with fakes injected for the generator + Cloudflare client so no Bun/workerd/CF
# is touched. Uses the shared ``beanie_test_db`` fixture (tests/ee/conftest.py)
# to init Beanie against an in-memory Mongo so the service can persist Site docs.
# Created: 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.5).
# Updated 2026-05-30 (security hardening, H3): added coverage that a malformed
# site_id on the authed paths raises NotFound (404) instead of leaking a raw
# bson InvalidId as an unhandled 500.
# Updated 2026-05-30 (follow-up item 4): coverage for list_domains — the new
# tenant-scoped read backing GET /sites/{site_id}/domains (returns the owning
# workspace's domains; a different workspace gets NotFound, i.e. a 404).
# Updated 2026-06-01 (Phase 2 — lead capture defaults): coverage that publish()
# seeds a default event_mapping (keyed on "lead") + default allowed_origins so a
# lead lands with no manual Mongo edit, and that add_domain() appends the custom
# hostname to allowed_origins.
# Updated 2026-06-01 (Phase 3 — local fake-deploy): coverage for the LOCAL deploy
# branch — with no CF creds and no injected CF client, publish() does NOT contact
# Cloudflare, persists the site via the local deployer, and stores+returns a local
# URL on the Site (and SiteResponse). Also asserts an injected CF client always
# wins (the real branch), so the existing CF tests keep exercising put_worker.
# Updated 2026-06-01 (Phase 4 — chat→create-site): coverage for publish_pocket(),
# the shared pocket-read + publish path the REST router and the in-process MCP
# tool both call — mocks pockets_service.get to prove it derives the theme from
# rippleSpec, falls back to the pocket's name, applies a name override, and
# propagates NotFound rather than swallowing it.
# Updated 2026-06-03 (Sites fix B — published site name defaults to the pocket
# name): coverage that publish() resolves a BLANK name to the source pocket's own
# display name (read via the pockets service's public get, no Beanie import) for
# both the stored Site.name and the generated site title, falling back to
# "Untitled site" only when the pocket has no name. Also pins the end-to-end
# publish_pocket path: a pocket named "Flower Shop Landing Page" published with no
# name lands a Site named "Flower Shop Landing Page".
# Updated 2026-06-18 (feat/branch-primitive-sites-draft, BP-2 / pocketpaw#1345):
# branch-aware publish/preview/status coverage over the BP-1 versions spine —
# (1) a pocket with only a draft version reads draft / not live; (2) is_live is
# True ONLY after a successful deploy and stays not-live when the deploy fails (the
# Site doc is not persisted) while the published version tag still stands;
# (3) preview_pocket serves the DRAFT VERSION's content (and falls back to current
# content when no draft row exists); (4) publish promotes the current draft to a
# published version via versions.publish; plus has_unpublished_changes when a draft
# is newer than the published version.
# Updated 2026-06-20 (DS-2 — dynamic-site D1 bindings): coverage that a
# pattern="dynamic" publish passes a d1 binding (name + database id) to
# put_worker and persists that id on the Site doc (reused on re-publish), that a
# static (landing/None) publish passes NO bindings (single-module path, regress
# guard), and that publish_pocket threads the pocket's pattern through.
# Updated 2026-06-25 (feat/sites-workers-deploy-mode): coverage for the 3-way
# deploy-mode selector — PAW_CF_DEPLOY_MODE=workers routes a STATIC publish to the
# injected workers deployer (and stamps the returned workers.dev url on the Site);
# a DYNAMIC site in workers mode raises a clean ValidationError (Phase 2); and with
# PAW_CF_DEPLOY_MODE UNSET the legacy local/wfp selection is preserved (an injected
# CF client still wins → put_worker; no env creds + no CF → local deployer).
#
# Updated 2026-06-25 (feat/sites-cf-dispatch-worker — published URL wiring):
# coverage that a CF-mode publish with PAW_CF_SITES_DOMAIN set stamps the Site
# doc's url as https://<site_id>.<domain> (and persists it), and that with the
# domain UNSET the CF path leaves url="" without crashing (the worker is still
# uploaded — the deploy succeeded — it just has no public URL until the operator
# sets the domain + deploys the dispatch worker).
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
        # DS-2: capture the bindings put_worker was called with so dynamic-site
        # tests can assert the d1 binding rode the deploy and static-site tests
        # can assert NO bindings (the single-module path) were passed.
        self.put_bindings = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        self.put_bindings.append(bindings)
        return True

    async def create_custom_hostname(self, hostname, *, features=None):
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
async def test_publish_local_mode_skips_cloudflare_and_returns_local_url(
    beanie_test_db, monkeypatch
):
    """Phase 3 Gap 2: with no CF creds and no injected CF client, publish() takes
    the LOCAL branch — it does NOT contact Cloudflare, persists the site via the
    local deployer, and stores+returns the served localhost URL on the Site. If
    the branch leaked to the CF path, _cf_client() would KeyError on the missing
    PAW_CF_ACCOUNT_ID — so reaching a clean local URL is itself the proof."""
    # No CF creds in the environment → _local_mode() is True.
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    deploy_calls: list[tuple[str, str]] = []

    def fake_local_deploy(site_id: str, project_dir: str) -> str:
        deploy_calls.append((site_id, project_dir))
        return f"http://127.0.0.1:9999/{site_id}/"

    gen = _FakeGenerator()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Local Site",
        _generator=gen,
        # NB: no _cloudflare injected — that's what selects the local branch.
        _bundle_reader=lambda d: b"unused-in-local-mode",
        _local_deploy=fake_local_deploy,
    )
    assert site.deployed is True
    # The local deployer was called with the site id + the generated project dir.
    assert deploy_calls == [(str(site.id), "/tmp/site")]
    # The local URL is persisted on the doc and points at the site id.
    assert site.url == f"http://127.0.0.1:9999/{site.id}/"
    # The DTO carries it too.
    resp = sites_service._to_response(site)
    assert resp.url == site.url


@pytest.mark.asyncio
async def test_publish_injected_cf_takes_real_branch_even_without_creds(
    beanie_test_db, monkeypatch
):
    """Phase 3 Gap 2 (additive, not a replacement): injecting a CF client forces
    the REAL Cloudflare branch even with no env creds, so the existing CF tests
    keep exercising put_worker and the local branch never hijacks them."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf = _FakeCF()

    def boom(site_id: str, project_dir: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("local deploy must not run when a CF client is injected")

    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=_FakeGenerator(),
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=boom,
    )
    assert cf.put_calls == [site.script_name]
    assert site.url == ""  # CF path leaves url empty when PAW_CF_SITES_DOMAIN unset


@pytest.mark.asyncio
async def test_publish_cf_stamps_subdomain_url_when_sites_domain_set(beanie_test_db, monkeypatch):
    """CF-DISPATCH: a published site is served at https://<site_id>.<domain> via the
    WfP dispatch worker (a user worker in a dispatch namespace is not directly
    URL-addressable). With PAW_CF_SITES_DOMAIN set, the CF branch stamps that
    per-site subdomain URL on the Site doc after put_worker, and it persists."""
    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "sites.example.com")
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,  # injected CF → the REAL deploy branch
        _bundle_reader=lambda d: b"export default {}",
    )
    expected = f"https://{site.id}.sites.example.com"
    # The deploy still uploaded the worker into the namespace.
    assert cf.put_calls == [site.script_name]
    # The URL is the per-site subdomain the dispatch worker resolves.
    assert site.url == expected
    # And it is persisted on the canonical Site doc (re-read from the DB).
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    reloaded = await _SiteDoc.find_one({"_id": ObjectId(site.script_name), "workspace": "ws1"})
    assert reloaded is not None
    assert reloaded.url == expected


@pytest.mark.asyncio
async def test_publish_cf_leaves_url_empty_when_sites_domain_unset(beanie_test_db, monkeypatch):
    """With PAW_CF_SITES_DOMAIN UNSET, the CF branch must not crash and must leave
    url="" — the worker is still uploaded (the deploy succeeded) but the site has no
    public URL until the operator sets the domain + deploys the dispatch worker."""
    monkeypatch.delenv("PAW_CF_SITES_DOMAIN", raising=False)
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,  # injected CF → the REAL deploy branch
        _bundle_reader=lambda d: b"export default {}",
    )
    assert cf.put_calls == [site.script_name]  # the worker WAS uploaded
    assert site.deployed is True  # the deploy succeeded
    assert site.url == ""  # …but there is no public URL without the domain


@pytest.mark.asyncio
async def test_publish_seeds_default_capture_config(beanie_test_db):
    """Phase 2: a freshly published site must be able to receive a lead with NO
    manual Mongo edit. publish() seeds a default event_mapping keyed on the
    "lead" form_type (the constant the generated endpoint sends) and default
    allowed_origins (the local dev hosts), so neither is empty."""
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
    # A "lead" mapping exists and creates a Lead with the common contact fields.
    assert "lead" in site.event_mapping
    assert site.event_mapping["lead"]["creates"] == "Lead"
    assert site.event_mapping["lead"]["fields"]["full_name"] == "{{ payload.full_name }}"
    assert site.event_mapping["lead"]["fields"]["phone"] == "{{ payload.phone }}"
    # Default origins are non-empty (origin_allowed fails closed on an empty list)
    # and include localhost so the local smoke can post from the served site.
    assert "localhost" in site.allowed_origins


@pytest.mark.asyncio
async def test_add_domain_appends_hostname_to_allowed_origins(beanie_test_db):
    """Phase 2: connecting a custom domain authorizes the site's own origin for
    capture — add_domain appends the hostname to allowed_origins (de-duped) so a
    lead from the production host is not 403'd, with no separate allow step."""
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
    # Re-read the persisted doc to confirm the origin was stored, not just held.
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    fresh = await _SiteDoc.get(site.id)
    assert "www.brightsmiledental.com" in fresh.allowed_origins
    # Idempotent: adding the same hostname again does not duplicate the origin.
    await sites_service.add_domain(
        workspace_id="ws1",
        site_id=site.id,
        hostname="www.brightsmiledental.com",
        _cloudflare=cf,
    )
    fresh2 = await _SiteDoc.get(site.id)
    assert fresh2.allowed_origins.count("www.brightsmiledental.com") == 1


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


@pytest.mark.asyncio
async def test_list_domains_returns_owning_workspace_domains(beanie_test_db):
    """Item 4: list_domains returns the site's domains (hostname, status,
    cname_target) for the owning workspace."""
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
    domains = await sites_service.list_domains(workspace_id="ws1", site_id=site.id)
    assert len(domains) == 1
    assert domains[0].hostname == "www.brightsmiledental.com"
    assert domains[0].status == "pending"
    assert domains[0].cname_target == "zone_1.cdn.cloudflare.net"


@pytest.mark.asyncio
async def test_list_domains_empty_when_no_domains_added(beanie_test_db):
    """A freshly published site with no custom domains lists an empty list."""
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
    assert await sites_service.list_domains(workspace_id="ws1", site_id=site.id) == []


@pytest.mark.asyncio
async def test_list_domains_cross_tenant_raises_not_found(beanie_test_db):
    """Tenant scoping: a different workspace cannot read this site's domains —
    _load raises NotFound (the router surfaces it as a 404)."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws_owner",
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
        workspace_id="ws_owner",
        site_id=site.id,
        hostname="www.example.com",
        _cloudflare=cf,
    )
    with pytest.raises(NotFound):
        await sites_service.list_domains(workspace_id="ws_other", site_id=site.id)


# ---------------------------------------------------------------------------
# publish_pocket — the shared pocket-read + publish path used by BOTH the REST
# router and the in-process MCP tool (Phase 4). Mocks pockets_service.get (the
# wire-dict reader) and injects generator + CF fakes, so it proves the shared
# function derives the theme from the pocket's rippleSpec and names the site.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_pocket_reads_pocket_and_delegates(beanie_test_db):
    """publish_pocket fetches the pocket via pockets_service.get, pulls the theme
    out of rippleSpec, falls back to the pocket's name, and persists a site."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {
        "name": "Bright Smile Dental",
        "rippleSpec": {"type": "container", "theme": {"primary": "#0A84FF"}},
    }
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ) as mock_get:
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    mock_get.assert_awaited_once_with("pk1", "u1")
    assert site.deployed is True
    assert site.pocket_id == "pk1"
    # name fell back to the pocket's own name.
    assert site.name == "Bright Smile Dental"
    # The theme pulled from rippleSpec.theme was handed to the generator.
    assert gen.built["theme"] == {"primary": "#0A84FF"}


@pytest.mark.asyncio
async def test_publish_pocket_name_override_wins(beanie_test_db):
    """An explicit name overrides the pocket's own name."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {"name": "Pocket Name", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            name="Override Name",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    assert site.name == "Override Name"


@pytest.mark.asyncio
async def test_publish_pocket_propagates_not_found(beanie_test_db):
    """When the pockets service raises NotFound (missing / access-denied), the
    shared path lets it propagate so callers (router → 404, MCP → is_error) can
    map it — publish_pocket does not swallow it."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    ):
        with pytest.raises(NotFound):
            await sites_service.publish_pocket(
                workspace_id="ws1",
                user_id="u1",
                pocket_id="pk_missing",
            )


# ---------------------------------------------------------------------------
# Fix B — a published site's name defaults to the source pocket's own name when
# the caller omits it (the publish schema promises this). Resolved in publish()
# itself (the source of truth) so a blank name never lands an unnamed site.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_blank_name_defaults_to_pocket_name(beanie_test_db):
    """publish() with a blank name looks up the source pocket (via the pockets
    service's public get) and uses its display name for BOTH the stored Site.name
    and the generated site title."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {"name": "Flower Shop Landing Page", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ) as mock_get:
        site = await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container"},
            theme={},
            # name deliberately omitted (defaults to "")
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    mock_get.assert_awaited_once_with("pk1", "u1")
    assert site.name == "Flower Shop Landing Page"
    # The generated site title used the same resolved name (not "Untitled site").
    assert gen.built["title"] == "Flower Shop Landing Page"


@pytest.mark.asyncio
async def test_publish_explicit_name_skips_pocket_lookup(beanie_test_db):
    """A non-blank name wins and publish() does NOT read the pocket for a name."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value={"name": "Pocket Name"}),
    ) as mock_get:
        site = await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container"},
            theme={},
            name="Explicit Name",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    mock_get.assert_not_awaited()
    assert site.name == "Explicit Name"
    assert gen.built["title"] == "Explicit Name"


@pytest.mark.asyncio
async def test_publish_blank_name_and_nameless_pocket_falls_back_to_untitled(
    beanie_test_db,
):
    """When the name is blank AND the pocket has no name, publish() falls back to
    'Untitled site' rather than persisting an empty name."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value={"name": ""}),
    ):
        site = await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container"},
            theme={},
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    assert site.name == "Untitled site"


@pytest.mark.asyncio
async def test_publish_pocket_no_name_lands_site_with_pocket_name(beanie_test_db):
    """End-to-end: publishing a pocket named 'Flower Shop Landing Page' WITHOUT a
    name (the shared publish_pocket path the REST + MCP surfaces use) results in a
    Site named 'Flower Shop Landing Page'."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {"name": "Flower Shop Landing Page", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            # no name passed — the agent / UI omitted it
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    assert site.name == "Flower Shop Landing Page"


# ---------------------------------------------------------------------------
# preview_pocket — the DRAFT-content reader backing
# GET /sites/by-pocket/{pocket_id}/preview. Reads the source pocket via the
# pockets service (the wire-dict reader, which raises NotFound itself) and
# returns {pocket_id, engine, content}: the rippleSpec for a ripple pocket, or
# the {path: contents} source map for a svelte pocket. Mirrors publish_pocket's
# pocket-read + engine logic so the preview matches what publish would build.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_pocket_ripple_returns_ripple_spec():
    """A ripple pocket previews its rippleSpec verbatim, with engine='ripple'."""
    from unittest.mock import AsyncMock, patch

    spec = {"type": "container", "ui": {"children": []}, "theme": {"primary": "#0A84FF"}}
    wire = {"name": "Bright Smile", "engine": "ripple", "rippleSpec": spec}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ) as mock_get:
        res = await sites_service.preview_pocket(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    mock_get.assert_awaited_once_with("pk1", "u1")
    assert res.pocket_id == "pk1"
    assert res.engine == "ripple"
    assert res.content == spec


@pytest.mark.asyncio
async def test_preview_pocket_defaults_engine_to_ripple():
    """A pocket with no explicit engine previews as ripple (the default track)."""
    from unittest.mock import AsyncMock, patch

    spec = {"type": "container"}
    wire = {"name": "x", "rippleSpec": spec}  # no engine key
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        res = await sites_service.preview_pocket(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    assert res.engine == "ripple"
    assert res.content == spec


@pytest.mark.asyncio
async def test_preview_pocket_svelte_returns_source_map():
    """A svelte pocket previews its {path: contents} source map, engine='svelte'."""
    from unittest.mock import AsyncMock, patch

    source = {
        "src/routes/+page.svelte": "<h1>Hello</h1>",
        "src/lib/Hero.svelte": "<section>hero</section>",
    }
    wire = {"name": "x", "engine": "svelte", "source": source, "rippleSpec": {}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        res = await sites_service.preview_pocket(workspace_id="ws1", user_id="u1", pocket_id="pk1")

    assert res.engine == "svelte"
    assert res.content == source


@pytest.mark.asyncio
async def test_preview_pocket_propagates_not_found():
    """A missing / access-denied pocket surfaces NotFound (router → 404), not a
    swallowed empty preview."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    ):
        with pytest.raises(NotFound):
            await sites_service.preview_pocket(
                workspace_id="ws1", user_id="u1", pocket_id="pk_missing"
            )


# ---------------------------------------------------------------------------
# pocket_status — the draft/published + is_live reader backing
# GET /sites/by-pocket/{pocket_id}/status. Derives the lifecycle from the Site
# deployment doc for the pocket (tenant-scoped on workspace): a Site doc means
# published (is_live == doc.deployed); no Site doc means draft / not live.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pocket_status_no_site_is_draft_not_live(beanie_test_db):
    """A pocket that has never been published has no Site doc → draft, not live."""
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_unpublished")
    assert res.pocket_id == "pk_unpublished"
    assert res.status == "draft"
    assert res.is_live is False
    assert res.site_id is None


@pytest.mark.asyncio
async def test_pocket_status_published_site_is_live(beanie_test_db):
    """A pocket with a deployed Site doc reads published + live, and carries the
    site id."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_live",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_live")
    assert res.pocket_id == "pk_live"
    assert res.status == "published"
    assert res.is_live is True
    assert res.site_id == str(site.id)


@pytest.mark.asyncio
async def test_pocket_status_is_tenant_scoped(beanie_test_db):
    """A Site published in another workspace does not leak into this workspace's
    status read — a different workspace sees the pocket as draft / not live."""
    gen, cf = _FakeGenerator(), _FakeCF()
    await sites_service.publish(
        workspace_id="ws_owner",
        user_id="u1",
        pocket_id="pk_x",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    res = await sites_service.pocket_status(workspace_id="ws_intruder", pocket_id="pk_x")
    assert res.status == "draft"
    assert res.is_live is False
    assert res.site_id is None


# ---------------------------------------------------------------------------
# BP-2 / #1345 — branch-aware publish/preview/status over the BP-1 versions
# spine. The bug: a site was stamped ``deployed`` (→ published + live) the
# instant a Site doc existed, so the "Live" badge lied and the preview pointed
# at the live URL. These pin the fix: status derives from the version pointers +
# real deploy state, preview serves the DRAFT version, publish promotes the draft
# to a published version before deploy.
# ---------------------------------------------------------------------------


class _BoomCF:
    """A Cloudflare client whose deploy (put_worker) FAILS — used to prove a
    failed deploy leaves the pocket not-live (no Site doc persisted) while the
    published version tag may still stand."""

    async def put_worker(self, *, script_name, bundle, bindings=None):
        raise RuntimeError("cloudflare put_worker failed")


@pytest.mark.asyncio
async def test_pocket_status_draft_version_only_is_draft_not_live(beanie_test_db):
    """A pocket that has a DRAFT version but was never published reads draft / not
    live, with has_unpublished_changes True (there is a draft to ship)."""
    from pocketpaw_ee.versions import service as versions_service

    await versions_service.write_draft(
        scope_type="pocket",
        scope_id="pk_draft_only",
        workspace_id="ws1",
        content={"type": "container"},
    )
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_draft_only")
    assert res.status == "draft"
    assert res.is_live is False
    assert res.has_unpublished_changes is True
    assert res.site_id is None


@pytest.mark.asyncio
async def test_publish_promotes_draft_to_published_version(beanie_test_db):
    """publish() promotes the pocket's current draft to a PUBLISHED version
    (versions.publish) before deploy, so a published pointer exists afterwards and
    the pocket reads published + live with no unpublished changes."""
    from pocketpaw_ee.versions import service as versions_service

    # A draft exists (as the pocket merge hook would have written).
    draft = await versions_service.write_draft(
        scope_type="pocket",
        scope_id="pk_promote",
        workspace_id="ws1",
        content={"type": "container", "v": 1},
    )
    assert draft.status == "draft"

    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_promote",
        ripple_spec={"type": "container", "v": 1},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    assert site.deployed is True

    # The draft was promoted: a published version pointer now exists, pointing at
    # the same version row (no NEW draft was created, the existing one flipped).
    published = await versions_service.get_published(scope_type="pocket", scope_id="pk_promote")
    assert published is not None
    assert str(published.id) == str(draft.id)
    assert published.status == "published"

    # Status derives published + live from the pointer + real deploy state.
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_promote")
    assert res.status == "published"
    assert res.is_live is True
    assert res.has_unpublished_changes is False
    assert res.site_id == str(site.id)


@pytest.mark.asyncio
async def test_publish_with_no_draft_writes_then_publishes_a_version(beanie_test_db):
    """A pocket published WITHOUT a pre-existing draft row (never went through
    merge_spec) still lands a published version: publish() writes a draft snapshot
    of the engine content, then promotes it."""
    from pocketpaw_ee.versions import service as versions_service

    assert await versions_service.get_published(scope_type="pocket", scope_id="pk_fresh") is None

    gen, cf = _FakeGenerator(), _FakeCF()
    await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_fresh",
        ripple_spec={"type": "container", "fresh": True},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    published = await versions_service.get_published(scope_type="pocket", scope_id="pk_fresh")
    assert published is not None
    assert published.content == {"type": "container", "fresh": True}


@pytest.mark.asyncio
async def test_publish_failed_deploy_leaves_pocket_not_live(beanie_test_db, monkeypatch):
    """is_live ONLY after a successful deploy: when the deploy raises, no Site doc
    is persisted (so pocket_status reads not-live), but the published version tag
    may still stand (published != live)."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
    from pocketpaw_ee.versions import service as versions_service

    # Force the REAL deploy branch (injected CF) and make it blow up.
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    gen = _FakeGenerator()
    boom_cf = _BoomCF()

    with pytest.raises(RuntimeError):
        await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk_fail",
            ripple_spec={"type": "container"},
            theme={},
            name="x",
            _generator=gen,
            _cloudflare=boom_cf,
            _bundle_reader=lambda d: b"x",
        )

    # No Site doc was persisted — the deploy failed before the insert.
    assert await _SiteDoc.find_one({"workspace": "ws1", "pocket_id": "pk_fail"}) is None

    # The pocket is NOT live (the only thing that earns a Live badge is a real
    # successful deploy, which never happened).
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_fail")
    assert res.is_live is False

    # The published version tag was set BEFORE the deploy, so it may stand:
    # published is true at the Branch layer even though the site is not live.
    published = await versions_service.get_published(scope_type="pocket", scope_id="pk_fail")
    assert published is not None
    assert res.status == "published"


@pytest.mark.asyncio
async def test_pocket_status_draft_newer_than_published_has_unpublished_changes(beanie_test_db):
    """After publishing, a NEW draft (a later edit) makes the pocket read published
    + live AND has_unpublished_changes — the edits a re-publish would ship."""
    from pocketpaw_ee.versions import service as versions_service

    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_edit",
        ripple_spec={"type": "container", "v": 1},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    # Right after publish: published, live, no unpublished changes.
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_edit")
    assert res.status == "published"
    assert res.is_live is True
    assert res.has_unpublished_changes is False

    # A later edit writes a NEW draft (as the merge hook would) newer than the
    # published version.
    await versions_service.write_draft(
        scope_type="pocket",
        scope_id="pk_edit",
        workspace_id="ws1",
        content={"type": "container", "v": 2},
    )
    res2 = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_edit")
    assert res2.status == "published"  # still live on the published version
    assert res2.is_live is True
    assert res2.has_unpublished_changes is True  # but there is a newer draft
    assert res2.site_id == str(site.id)


@pytest.mark.asyncio
async def test_pocket_status_legacy_deployed_site_without_versions_reads_published(
    beanie_test_db,
):
    """Backward compat: a Site deployed BEFORE BP-1 has a deployed Site doc but no
    version rows — it must still read published + live, not regress to draft."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    legacy = _SiteDoc(
        workspace="ws1",
        pocket_id="pk_legacy",
        owner="u1",
        name="Legacy",
        script_name="legacy",
        deployed=True,
    )
    await legacy.insert()
    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_legacy")
    assert res.status == "published"
    assert res.is_live is True
    assert res.has_unpublished_changes is False
    assert res.site_id == str(legacy.id)


@pytest.mark.asyncio
async def test_preview_pocket_serves_draft_version_content(beanie_test_db):
    """preview_pocket serves the DRAFT VERSION's content (the unpublished working
    copy), NOT the pocket's currently-stored rippleSpec, so the Preview tab shows
    what publish WOULD build."""
    from unittest.mock import AsyncMock, patch

    from pocketpaw_ee.versions import service as versions_service

    # The pocket's CURRENT stored spec (e.g. what was last published / saved).
    current_spec = {"type": "container", "v": "current"}
    # A newer DRAFT version — the working copy the preview must serve.
    draft_spec = {"type": "container", "v": "draft"}
    await versions_service.write_draft(
        scope_type="pocket",
        scope_id="pk_preview",
        workspace_id="ws1",
        content=draft_spec,
    )

    wire = {"name": "x", "engine": "ripple", "rippleSpec": current_spec}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        res = await sites_service.preview_pocket(
            workspace_id="ws1", user_id="u1", pocket_id="pk_preview"
        )

    assert res.engine == "ripple"
    # The draft snapshot wins over the pocket's current stored spec.
    assert res.content == draft_spec


@pytest.mark.asyncio
async def test_preview_pocket_falls_back_to_current_content_when_no_draft(beanie_test_db):
    """When no draft version row exists (a pre-BP-1 pocket, or a svelte pocket whose
    source map BP-1 does not version), preview falls back to the pocket's current
    content so the preview is never empty when content exists."""
    from unittest.mock import AsyncMock, patch

    current_spec = {"type": "container", "v": "current"}
    wire = {"name": "x", "engine": "ripple", "rippleSpec": current_spec}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        res = await sites_service.preview_pocket(
            workspace_id="ws1", user_id="u1", pocket_id="pk_no_draft"
        )

    assert res.content == current_spec


# ---------------------------------------------------------------------------
# DS-2 — a DYNAMIC site (Pocket.pattern == "dynamic") is backed by a per-tenant
# Cloudflare D1. Its deployed Worker therefore needs a D1 binding so the SSR
# remote functions can reach that DB. publish() passes the d1 binding(s) to
# put_worker ONLY when the site is dynamic; a static (landing/None) publish
# passes NO bindings (the single-module path stays byte-for-byte unchanged).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_publish_passes_d1_binding_to_put_worker(beanie_test_db):
    """A pattern="dynamic" publish hands put_worker a d1 binding carrying the
    binding name + the site's D1 database id, and persists that id on the Site
    doc so a re-publish reuses it (and DS-3 can read the site's D1)."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_dyn",
        ripple_spec={
            "type": "container",
            "objects": [{"name": "signups", "fields": {"email": "text"}}],
            "sources": [
                {"name": "all", "kind": "data", "object": "signups", "refresh": "pocket_open"}
            ],
        },
        theme={"primary": "#0A84FF"},
        name="Guestbook",
        pattern="dynamic",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    assert site.deployed is True
    # put_worker received exactly one call carrying a non-empty bindings list.
    assert len(cf.put_bindings) == 1
    bindings = cf.put_bindings[0]
    assert bindings, "dynamic publish must pass bindings to put_worker"
    d1 = [b for b in bindings if b.get("type") == "d1"]
    assert len(d1) == 1
    assert d1[0]["name"] == "DB"
    assert d1[0]["id"], "d1 binding must carry the database id"
    # The id is persisted on the Site doc (re-publish reuse + DS-3 read).
    assert site.d1_database_id == d1[0]["id"]


@pytest.mark.asyncio
async def test_dynamic_publish_reuses_persisted_d1_id_on_republish(beanie_test_db):
    """Re-publishing a dynamic pocket keeps the SAME D1 id (the binding target
    must be stable across deploys — the data lives behind that id)."""
    gen, cf = _FakeGenerator(), _FakeCF()
    kw = dict(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_dyn2",
        ripple_spec={
            "type": "container",
            "objects": [{"name": "rows", "fields": {"x": "text"}}],
            "actions": [{"name": "add", "object": "rows", "op": "insert"}],
        },
        theme={"primary": "#000"},
        name="Board",
        pattern="dynamic",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    first = await sites_service.publish(**kw)
    second = await sites_service.publish(**kw)
    assert first.d1_database_id
    assert first.d1_database_id == second.d1_database_id


@pytest.mark.asyncio
async def test_static_publish_passes_no_bindings(beanie_test_db):
    """A static site (pattern None / 'landing') must NOT pass bindings — the
    single-module upload path is unchanged (regress guard)."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_static",
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        name="Bright Smile",
        pattern="landing",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    assert site.deployed is True
    assert cf.put_bindings == [None]
    assert site.d1_database_id == ""


@pytest.mark.asyncio
async def test_publish_pocket_threads_dynamic_pattern(beanie_test_db):
    """publish_pocket reads pattern off the pocket wire dict and forwards it, so a
    dynamic pocket published via the shared path gets the d1 binding."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {
        "name": "Live Guestbook",
        "pattern": "dynamic",
        "rippleSpec": {
            "type": "container",
            "objects": [{"name": "g", "fields": {"who": "text"}}],
            "sources": [{"name": "all", "kind": "data", "object": "g", "refresh": "pocket_open"}],
        },
    }
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk_dyn3",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    assert site.d1_database_id
    d1 = [b for b in (cf.put_bindings[0] or []) if b.get("type") == "d1"]
    assert len(d1) == 1


# ── workers deploy mode (PAW_CF_DEPLOY_MODE=workers) ──────────────────────────


@pytest.mark.asyncio
async def test_workers_mode_routes_static_publish_to_workers_deployer(beanie_test_db, monkeypatch):
    """PAW_CF_DEPLOY_MODE=workers routes a STATIC publish to the injected workers
    deployer (NOT put_worker, NOT the local server) and stamps the returned
    workers.dev URL on the Site doc."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    calls: list[tuple[str, str]] = []

    async def fake_workers_deploy(site_id: str, project_dir: str) -> str:
        calls.append((site_id, project_dir))
        return f"https://paw-site-{site_id}.acct.workers.dev"

    def cf_boom(*a, **k):  # pragma: no cover - must not run in workers mode
        raise AssertionError("workers mode must not build a real CF client")

    gen = _FakeGenerator()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_w1",
        ripple_spec={"type": "container"},
        theme={},
        name="Workers Site",
        _generator=gen,
        # No _cloudflare injected → the workers branch owns the deploy.
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=fake_workers_deploy,
    )

    assert site.deployed is True
    assert calls == [(str(site.id), "/tmp/site")]
    assert site.url == f"https://paw-site-{site.id}.acct.workers.dev"
    resp = sites_service._to_response(site)
    assert resp.url == site.url


@pytest.mark.asyncio
async def test_workers_mode_dynamic_site_raises_validation_error(beanie_test_db, monkeypatch):
    """A DYNAMIC site in workers mode is rejected with a clean ValidationError
    (Phase 2 needs a per-tenant D1) rather than deploying a broken site. The
    injected workers deployer must NOT be called."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    async def must_not_run(site_id: str, project_dir: str) -> str:  # pragma: no cover
        raise AssertionError("a dynamic site must not reach the workers deployer")

    gen = _FakeGenerator()
    with pytest.raises(ValidationError) as exc:
        await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk_w_dyn",
            ripple_spec={
                "type": "container",
                "objects": [{"name": "rows", "fields": {"x": "text"}}],
                "actions": [{"name": "add", "object": "rows", "op": "insert"}],
            },
            theme={},
            name="Dynamic in Workers",
            pattern="dynamic",
            _generator=gen,
            _bundle_reader=lambda d: b"unused",
            _workers_deploy=must_not_run,
        )
    assert "workers" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_unset_deploy_mode_preserves_legacy_cf_branch(beanie_test_db, monkeypatch):
    """With PAW_CF_DEPLOY_MODE UNSET, an injected CF client takes the legacy WfP
    (put_worker) branch — the workers deployer must NOT run."""
    monkeypatch.delenv("PAW_CF_DEPLOY_MODE", raising=False)
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf = _FakeCF()

    async def workers_boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("workers deployer must not run with mode unset + CF injected")

    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_legacy_cf",
        ripple_spec={"type": "container"},
        theme={},
        name="Legacy CF",
        _generator=_FakeGenerator(),
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _workers_deploy=workers_boom,
    )
    assert cf.put_calls == [site.script_name]
    assert site.url == ""  # CF/WfP path leaves url empty in v1


@pytest.mark.asyncio
async def test_unset_deploy_mode_preserves_legacy_local_branch(beanie_test_db, monkeypatch):
    """With PAW_CF_DEPLOY_MODE UNSET and no CF creds / no CF client, publish() still
    takes the legacy LOCAL branch — the workers deployer must NOT run."""
    monkeypatch.delenv("PAW_CF_DEPLOY_MODE", raising=False)
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    def fake_local_deploy(site_id: str, project_dir: str) -> str:
        return f"http://127.0.0.1:9999/{site_id}/"

    async def workers_boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("workers deployer must not run in legacy local mode")

    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk_legacy_local",
        ripple_spec={"type": "container"},
        theme={},
        name="Legacy Local",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused",
        _local_deploy=fake_local_deploy,
        _workers_deploy=workers_boom,
    )
    assert site.url == f"http://127.0.0.1:9999/{site.id}/"
