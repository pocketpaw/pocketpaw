# tests/ee/sites/test_sites_first_publish_slug.py
# Created: 2026-09-23 (VS-2, feat/sites-first-publish-slug) — a NEW site on the
# ``workers`` lane publishes at an address built from its name, and nothing else moves.
#
# The contract, one test (or more) per line:
#   * a first publish claims ``slug`` + ``worker_name`` from the site's name and
#     deploys the Worker under it;
#   * the same name again gets ``-2``; a name already among the account's Worker
#     scripts is skipped; a ``DuplicateKeyError`` on the unique ``slug`` index (a race
#     the taken-check could not see) moves on to the next candidate;
#   * a LEGACY site (deployed, no stored name) keeps ``paw-site-<id>`` and gets no slug;
#   * a slugged site republishes under its own name;
#   * a stored-but-never-deployed name that already exists in the account is REFUSED
#     (``sites.worker_name_conflict``), never overwritten;
#   * the WfP lane claims nothing.
#
# MUTATIONS THAT BREAK THIS FILE (tests/mutations/sites_first_publish_slug.json):
# dropping the DuplicateKeyError retry, claiming a slug on a legacy republish, removing
# the pre-deploy guard, and skipping the account-script check.
#
# Updated 2026-09-23 (VS-2 review): a name this row CLAIMED (slug == worker_name) is
# never guarded, so a retry after a first deploy that failed once wrangler had created
# the script deploys under it instead of 409-ing forever. The guard now applies only to
# a stored name with no matching slug, and the account listing needs no zone id.
#
# Every publish test pins PAW_CF_DEPLOY_MODE and replaces ``account_script_names``:
# python-dotenv can climb out of the worktree and load real PAW_CF_* values.
from __future__ import annotations

import httpx
import pytest
from bson import ObjectId
from pocketpaw_ee.cloud._core.errors import ConflictError
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

WS = "ws-slug"


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _Deployer:
    def __init__(self):
        self.names: list[str | None] = []

    async def __call__(self, site_id, project_dir, *, worker_name=None, **_):
        self.names.append(worker_name)
        return f"https://{worker_name}.acct.workers.dev"


@pytest.fixture
def account(monkeypatch):
    """The Cloudflare account's Worker scripts, as the publish path sees them."""
    scripts: set[str] = set()

    async def _names(*, refresh: bool = False) -> frozenset[str]:
        return frozenset(scripts)

    monkeypatch.setattr(sites_service, "account_script_names", _names)
    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    return scripts


async def _publish(pocket_id: str, deployer, *, name: str = "Acme Bakery", workspace=WS):
    return await sites_service.publish(
        workspace_id=workspace,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name=name,
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"unused-in-workers-mode",
        _workers_deploy=deployer,
    )


async def _row(site_id) -> dict:
    # Re-read from the collection: mongomock hands back docs by reference, so a field
    # read off the returned object can reflect a write that never persisted.
    return await _SiteDoc.get_pymongo_collection().find_one({"_id": ObjectId(str(site_id))})


# ── first publish ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_site_publishes_at_its_name(beanie_test_db, account):
    deploy = _Deployer()

    site = await _publish("pk-acme", deploy)
    row = await _row(site.id)

    assert deploy.names == ["acme-bakery"]
    assert row["slug"] == "acme-bakery"
    assert row["worker_name"] == "acme-bakery"
    assert row["url"] == "https://acme-bakery.acct.workers.dev"
    assert row["deployed"] is True
    # The row the claim inserted before the deploy carries the same capture config a
    # first publish always seeded, and the upsert kept it.
    assert row["signed_key"]
    assert row["event_mapping"]
    assert "acme-bakery.acct.workers.dev" in row["allowed_origins"]


@pytest.mark.asyncio
async def test_a_second_site_with_the_same_name_gets_dash_two(beanie_test_db, account):
    await _publish("pk-acme-1", _Deployer())
    deploy = _Deployer()

    site = await _publish("pk-acme-2", deploy, workspace="ws-other")
    row = await _row(site.id)

    assert deploy.names == ["acme-bakery-2"]
    assert (row["slug"], row["worker_name"]) == ("acme-bakery-2", "acme-bakery-2")


@pytest.mark.asyncio
async def test_a_name_already_in_the_account_is_skipped(beanie_test_db, account):
    account.add("acme-bakery")
    deploy = _Deployer()

    site = await _publish("pk-acme", deploy)

    assert deploy.names == ["acme-bakery-2"]
    assert (await _row(site.id))["slug"] == "acme-bakery-2"


@pytest.mark.asyncio
async def test_an_unusable_name_gets_a_site_fallback(beanie_test_db, account):
    deploy = _Deployer()

    site = await _publish("pk-paw", deploy, name="paw-sites-dispatch")
    row = await _row(site.id)

    assert row["slug"].startswith("site-")
    assert deploy.names == [row["slug"]]


# ── the race on the unique index ─────────────────────────────────────────────


def _blind_taken_check(monkeypatch, blind_to: str):
    """Make the "is this address held by another site?" read miss ``blind_to``, the
    way a concurrent publish that claimed it a moment later would. The unique index is
    then the only thing between two sites and one address."""
    real = _SiteDoc.find_one

    async def _none():
        return None

    def _find_one(*args, **kwargs):
        if args and args[0] == {"slug": blind_to}:
            return _none()
        return real(*args, **kwargs)

    monkeypatch.setattr(_SiteDoc, "find_one", staticmethod(_find_one))


async def _seed_holder(slug: str) -> None:
    await _SiteDoc(
        id=ObjectId(),
        workspace="ws-holder",
        pocket_id="pk-holder",
        owner="u0",
        name="Holder",
        slug=slug,
        worker_name=slug,
        deployed=True,
        url=f"https://{slug}.acct.workers.dev",
    ).insert()


@pytest.mark.asyncio
async def test_a_duplicate_key_on_first_insert_moves_to_the_next_candidate(
    beanie_test_db, account, monkeypatch
):
    await _seed_holder("acme-bakery")
    _blind_taken_check(monkeypatch, "acme-bakery")
    deploy = _Deployer()

    site = await _publish("pk-acme", deploy)

    assert deploy.names == ["acme-bakery-2"]
    assert (await _row(site.id))["slug"] == "acme-bakery-2"


@pytest.mark.asyncio
async def test_a_duplicate_key_on_an_existing_row_moves_to_the_next_candidate(
    beanie_test_db, account, monkeypatch
):
    """A draft row (created before publish, never deployed) takes the update path."""
    oid = sites_service._live_object_id(WS, "pk-draft")
    await _SiteDoc(
        id=oid, workspace=WS, pocket_id="pk-draft", owner="u1", name="Acme Bakery", url=""
    ).insert()
    await _seed_holder("acme-bakery")
    _blind_taken_check(monkeypatch, "acme-bakery")
    deploy = _Deployer()

    await _publish("pk-draft", deploy)

    assert deploy.names == ["acme-bakery-2"]
    assert (await _row(oid))["slug"] == "acme-bakery-2"


# ── legacy and republish ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_legacy_site_republishes_under_its_derived_name(beanie_test_db, account):
    oid = sites_service._live_object_id(WS, "pk-legacy")
    await _SiteDoc(
        id=oid,
        workspace=WS,
        pocket_id="pk-legacy",
        owner="u1",
        name="Acme Bakery",
        script_name=str(oid),
        deployed=True,
        deploy_target="workers",
        url=f"https://paw-site-{oid}.acct.workers.dev",
    ).insert()
    deploy = _Deployer()

    await _publish("pk-legacy", deploy)
    row = await _row(oid)

    assert deploy.names == [f"paw-site-{oid}"]
    assert row.get("slug") is None
    assert row.get("worker_name") is None


@pytest.mark.asyncio
async def test_a_slugged_site_republishes_under_its_own_name(beanie_test_db, account):
    first = _Deployer()
    site = await _publish("pk-acme", first)
    # Its own Worker now exists in the account; that must not trip the guard.
    account.add("acme-bakery")
    again = _Deployer()

    await _publish("pk-acme", again, name="Renamed Bakery")
    row = await _row(site.id)

    assert again.names == ["acme-bakery"]
    assert (row["slug"], row["worker_name"]) == ("acme-bakery", "acme-bakery")


# ── the pre-deploy guard ─────────────────────────────────────────────────────


async def _seed_undeployed_named(
    pocket_id: str, worker_name: str, *, slug: str | None = None
) -> ObjectId:
    oid = sites_service._live_object_id(WS, pocket_id)
    await _SiteDoc(
        id=oid,
        workspace=WS,
        pocket_id=pocket_id,
        owner="u1",
        name="Acme Bakery",
        worker_name=worker_name,
        slug=slug,
        deployed=False,
        url="",
    ).insert()
    return oid


@pytest.mark.asyncio
async def test_the_guard_refuses_to_overwrite_a_foreign_script(beanie_test_db, account):
    """A stored name that was NOT claimed through the slug path (no matching ``slug``,
    e.g. set by an operator) and already exists in the account is not provably ours."""
    await _seed_undeployed_named("pk-guard", "taken-name")
    account.add("taken-name")
    deploy = _Deployer()

    with pytest.raises(ConflictError) as exc:
        await _publish("pk-guard", deploy)

    assert exc.value.code == "sites.worker_name_conflict"
    assert deploy.names == []


@pytest.mark.asyncio
async def test_a_stored_name_not_in_the_account_deploys(beanie_test_db, account):
    """The retry after a deploy that failed before creating its Worker."""
    oid = await _seed_undeployed_named("pk-retry", "free-name")
    deploy = _Deployer()

    await _publish("pk-retry", deploy)

    assert deploy.names == ["free-name"]
    assert (await _row(oid))["url"] == "https://free-name.acct.workers.dev"


class _FailsAfterCreating(_Deployer):
    """wrangler created the script in the account, then the deploy failed."""

    def __init__(self, account: set[str]):
        super().__init__()
        self._account = account

    async def __call__(self, site_id, project_dir, *, worker_name=None, **_):
        self.names.append(worker_name)
        self._account.add(worker_name)
        raise RuntimeError("wrangler: upload of assets failed")


@pytest.mark.asyncio
async def test_a_retry_after_a_failed_first_deploy_reuses_its_own_worker(beanie_test_db, account):
    """The first deploy left ``acme-bakery`` in the account and the row undeployed. That
    script is this site's own; the retry must deploy under it, not 409."""
    failing = _FailsAfterCreating(account)
    with pytest.raises(Exception, match="wrangler"):
        await _publish("pk-acme", failing)
    assert failing.names == ["acme-bakery"]
    assert "acme-bakery" in account

    retry = _Deployer()
    site = await _publish("pk-acme", retry)
    row = await _row(site.id)

    assert retry.names == ["acme-bakery"]
    assert (row["slug"], row["worker_name"]) == ("acme-bakery", "acme-bakery")
    assert row["url"] == "https://acme-bakery.acct.workers.dev"


@pytest.mark.asyncio
async def test_a_claimed_name_in_the_account_is_not_refused(beanie_test_db, account):
    """Same state seeded directly: a claimed, never-deployed name present in the account."""
    oid = await _seed_undeployed_named("pk-own", "own-name", slug="own-name")
    account.add("own-name")
    deploy = _Deployer()

    await _publish("pk-own", deploy)

    assert deploy.names == ["own-name"]
    assert (await _row(oid))["url"] == "https://own-name.acct.workers.dev"


# ── the WfP lane ─────────────────────────────────────────────────────────────


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


@pytest.mark.asyncio
async def test_the_wfp_lane_claims_no_address(beanie_test_db, account, monkeypatch):
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "wfp")

    site = await sites_service.publish(
        workspace_id=WS,
        user_id="u1",
        pocket_id="pk-wfp",
        ripple_spec={"type": "container"},
        theme={},
        name="Acme Bakery",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda _d: b"x",
    )
    row = await _row(site.id)

    assert row.get("slug") is None
    assert row.get("worker_name") is None


# ── listing the account's scripts ────────────────────────────────────────────


def _cf(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_list_account_scripts_reads_the_account_level_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={"success": True, "result": [{"id": "paw-sites-dispatch"}, {"id": "acme"}]},
        )

    names = await _cf(handler).list_account_scripts()

    assert (seen["method"], seen["path"]) == ("GET", "/client/v4/accounts/acct_1/workers/scripts")
    assert names == ["paw-sites-dispatch", "acme"]


@pytest.mark.asyncio
async def test_account_script_names_caches_and_fails_open(monkeypatch):
    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    calls: list[int] = []

    class _Client:
        async def list_account_scripts(self):
            calls.append(1)
            return ["acme"]

    monkeypatch.setattr(sites_service, "_cf_account_client", lambda: _Client())
    assert await sites_service.account_script_names() == {"acme"}
    assert await sites_service.account_script_names() == {"acme"}
    assert len(calls) == 1
    assert await sites_service.account_script_names(refresh=True) == {"acme"}
    assert len(calls) == 2

    def _unconfigured():
        raise RuntimeError("Cloudflare is not configured")

    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    monkeypatch.setattr(sites_service, "_cf_account_client", _unconfigured)
    assert await sites_service.account_script_names() == frozenset()


@pytest.mark.asyncio
async def test_the_account_check_runs_without_a_zone_id(monkeypatch):
    """Listing the account's scripts needs the account id and token only. Requiring
    PAW_CF_ZONE_ID (as ``_cf_client`` does) silently skipped the check."""
    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.setenv("PAW_CF_API_TOKEN", "tok_1")
    monkeypatch.delenv("PAW_CF_ZONE_ID", raising=False)

    async def _list(self):
        assert self._account_id == "acct_1"
        return ["acme"]

    monkeypatch.setattr(CloudflareClient, "list_account_scripts", _list)

    assert await sites_service.account_script_names(refresh=True) == {"acme"}


@pytest.mark.asyncio
async def test_no_account_credentials_fails_open(monkeypatch):
    monkeypatch.setattr(sites_service, "_account_scripts_cache", None)
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "")
    monkeypatch.setenv("PAW_CF_API_TOKEN", "")

    async def _list(self):  # pragma: no cover - must not be reached
        raise AssertionError("listed without credentials")

    monkeypatch.setattr(CloudflareClient, "list_account_scripts", _list)

    assert await sites_service.account_script_names(refresh=True) == frozenset()
