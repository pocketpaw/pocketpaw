# tests/cloud/jobs/test_provision_site.py
# Created: 2026-07-09 (feat/dp0-provision-job, DP0-3) — integration coverage for the
# durable ``provision_site`` job, mirroring the jobs tests' pytest-asyncio + mongo_db
# conventions. Externals are mocked (CF client's create_database / put_worker, the
# GeneratorClient build via the sites-service build seam, and the wrangler-migrate
# helper); the REAL Site-doc seams + real pockets-service reads run against the
# in-memory mongomock DB so the persistence contract is asserted for real.
#
# Pins the DP0-3 acceptance criteria:
#   * guarded create is a NO-OP when Site.d1_database_id is already set (create_database
#     not called, the stored id is reused for build + binding);
#   * a mid-job failure (migrate raises) leaves provision_status == "failed" AND the
#     freshly-created d1_database_id persisted — proving a retry reuses that D1 and
#     never orphans a second one;
#   * success writes provision_status="provisioned" + deployed=True to the Site doc
#     and returns the state-only partial {"state": {"provision_status": "done"}};
#   * the job is registered under the name "provision_site".

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.jobs.builtin import register_builtins
from pocketpaw_ee.cloud.jobs.builtin.provision_site import ProvisionSiteJob
from pocketpaw_ee.cloud.jobs.registry import resolve_job
from pocketpaw_ee.sites import d1_migrate
from pocketpaw_ee.sites import service as sites_service

WS = "ws-provision-1"
OTHER_WS = "ws-provision-2"


class _FakeCF:
    """A stand-in Cloudflare client recording its create_database / put_worker calls.

    ``create_database`` returns a fixed fresh uuid; ``put_worker`` records the bindings
    so a test can assert the D1 id threaded into the deploy binding."""

    def __init__(self, new_id: str = "d1-fresh-uuid-0001") -> None:
        self.new_id = new_id
        self.create_calls: list[str] = []
        self.put_calls: list[dict[str, Any]] = []

    async def create_database(self, name: str) -> str:
        self.create_calls.append(name)
        return self.new_id

    async def put_worker(self, *, script_name: str, bundle: bytes, bindings: Any) -> bool:
        self.put_calls.append({"script_name": script_name, "bundle": bundle, "bindings": bindings})
        return True


@pytest_asyncio.fixture
async def seed(mongo_db: Any):
    """Insert a real dynamic Pocket + its canonical Site doc, return a small context.

    The Site is created at the STABLE per-(workspace, pocket) id (the same identity a
    live publish upserts), in ``provision_status="provisioning"`` — the state the job
    picks up. ``d1_database_id`` is caller-chosen so a test can exercise the guarded
    (already-provisioned) branch or the fresh-create branch."""
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.cloud.models.site import Site

    async def _seed(*, workspace: str = WS, d1_database_id: str = "") -> dict[str, Any]:
        spec = {
            "theme": {"mode": "light"},
            "objects": [{"name": "entries", "fields": [{"name": "msg"}]}],
            "sources": [{"name": "list_entries"}],
            "actions": [{"name": "add_entry"}],
        }
        pocket = Pocket(workspace=workspace, name="Guestbook", owner="owner-1", rippleSpec=spec)
        await pocket.insert()
        pocket_id = str(pocket.id)

        site = Site(
            id=sites_service._live_object_id(workspace, pocket_id),
            workspace=workspace,
            pocket_id=pocket_id,
            owner="owner-1",
            name="Guestbook",
            signed_key="site_key_test",
            provision_status="provisioning",
            d1_database_id=d1_database_id,
        )
        await site.insert()
        return {"pocket_id": pocket_id, "site_id": str(site.id), "spec": spec}

    return _seed


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cf: _FakeCF,
    migrate_raises: Exception | None = None,
) -> dict[str, list]:
    """Monkeypatch the three externals — the CF client, the build seam, and the
    wrangler-migrate helper — and return a record of the build-seam calls so a test
    can assert the D1 id threaded into the build."""
    build_calls: list[dict[str, Any]] = []
    migrate_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(sites_service, "provision_cf_client", lambda: cf)

    async def _fake_build(*, site: Any, ripple_spec: Any, d1_database_id: str, **_kw: Any):
        build_calls.append({"site_id": str(site.id), "d1_database_id": d1_database_id})
        return "/fake/project/dir", b"worker-bundle-bytes"

    monkeypatch.setattr(sites_service, "build_provision_bundle", _fake_build)

    async def _fake_migrate(site_id: str, project_dir: str) -> None:
        migrate_calls.append((site_id, project_dir))
        if migrate_raises is not None:
            raise migrate_raises

    monkeypatch.setattr(d1_migrate, "apply_migrations", _fake_migrate)
    return {"build": build_calls, "migrate": migrate_calls}


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_provision_site_job_is_registered() -> None:
    register_builtins()
    job = resolve_job("provision_site")
    assert isinstance(job, ProvisionSiteJob)
    assert job.name == "provision_site"


# ---------------------------------------------------------------------------
# Guarded create is a no-op when the D1 id is already set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guarded_create_reuses_existing_d1(
    seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pocketpaw_ee.cloud.models.site import Site

    ctx = await seed(d1_database_id="already-provisioned-d1")
    cf = _FakeCF()
    rec = _install_fakes(monkeypatch, cf=cf)

    result = await ProvisionSiteJob()(
        workspace_id=WS, pocket_id=ctx["pocket_id"], job_id="job-1", params={}
    )

    # create_database is NOT called — the stored id is reused everywhere.
    assert cf.create_calls == []
    assert rec["build"][0]["d1_database_id"] == "already-provisioned-d1"
    assert cf.put_calls[0]["bindings"] == [
        {"type": "d1", "name": "DB", "id": "already-provisioned-d1"}
    ]

    site = await Site.get(ctx["site_id"])
    assert site is not None
    assert site.provision_status == "provisioned"
    assert site.deployed is True
    assert site.d1_database_id == "already-provisioned-d1"
    assert result == {"state": {"provision_status": "done"}}


# ---------------------------------------------------------------------------
# Mid-job failure (migrate raises) → failed, but the created D1 id is persisted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_failure_marks_failed_but_persists_d1(
    seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pocketpaw_ee.cloud.models.site import Site

    ctx = await seed(d1_database_id="")  # no prior D1 — the job creates one
    cf = _FakeCF(new_id="d1-fresh-uuid-0001")
    _install_fakes(monkeypatch, cf=cf, migrate_raises=RuntimeError("migrate blew up"))

    with pytest.raises(RuntimeError, match="migrate blew up"):
        await ProvisionSiteJob()(
            workspace_id=WS, pocket_id=ctx["pocket_id"], job_id="job-2", params={}
        )

    # The D1 was created under the paw-site-<siteId> name.
    assert cf.create_calls == [d1_migrate.database_name(ctx["site_id"])]

    site = await Site.get(ctx["site_id"])
    assert site is not None
    # Failed — but the id is persisted so a retry reuses it (no orphaned second D1).
    assert site.provision_status == "failed"
    assert site.d1_database_id == "d1-fresh-uuid-0001"
    # The Worker was never deployed (failure was before put_worker).
    assert cf.put_calls == []


# ---------------------------------------------------------------------------
# Success → provisioned + deployed, state-only partial returned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_marks_provisioned_and_deployed(
    seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pocketpaw_ee.cloud.models.site import Site

    ctx = await seed(d1_database_id="")
    cf = _FakeCF(new_id="d1-fresh-uuid-0002")
    rec = _install_fakes(monkeypatch, cf=cf)

    result = await ProvisionSiteJob()(
        workspace_id=WS, pocket_id=ctx["pocket_id"], job_id="job-3", params={}
    )

    # Fresh create → build/migrate/deploy all keyed on the new id.
    assert cf.create_calls == [d1_migrate.database_name(ctx["site_id"])]
    assert rec["build"][0]["d1_database_id"] == "d1-fresh-uuid-0002"
    assert rec["migrate"] == [(ctx["site_id"], "/fake/project/dir")]
    assert cf.put_calls[0]["script_name"] == ctx["site_id"]
    assert cf.put_calls[0]["bundle"] == b"worker-bundle-bytes"
    assert cf.put_calls[0]["bindings"] == [{"type": "d1", "name": "DB", "id": "d1-fresh-uuid-0002"}]

    site = await Site.get(ctx["site_id"])
    assert site is not None
    assert site.provision_status == "provisioned"
    assert site.deployed is True
    assert site.d1_database_id == "d1-fresh-uuid-0002"
    assert result == {"state": {"provision_status": "done"}}


# ---------------------------------------------------------------------------
# Fail-closed tenancy re-check — a pocket in another workspace never provisions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenancy_mismatch_fails_closed(seed: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await seed(workspace=OTHER_WS, d1_database_id="")
    cf = _FakeCF()
    _install_fakes(monkeypatch, cf=cf)

    # The job runs for WS but the pocket lives in OTHER_WS → refuse before any work.
    with pytest.raises(Exception, match="tenancy mismatch"):
        await ProvisionSiteJob()(
            workspace_id=WS, pocket_id=ctx["pocket_id"], job_id="job-4", params={}
        )

    assert cf.create_calls == []
    assert cf.put_calls == []
