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
    assert site.url == ""  # CF path leaves url empty in v1


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
