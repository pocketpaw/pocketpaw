# tests/ee/sites/test_draft_worker_service.py: where the draft Worker meets the sites
# service: a successful publish purges every draft of the pocket (and a failed purge
# never fails the publish), the builder shows the published site for the published
# content instead of rebuilding a draft, and site / pocket delete remove drafts too.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import delete_cascade, draft_worker, project_build
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.test_delete_cascade import _Deps, _saver
from tests.ee.sites.test_delete_cascade import _Site as _CascadeSite
from tests.ee.sites.test_project_engine import (
    SOURCE,
    WORKER_MANIFEST,
    _bundle,
    _make_project_pocket,
    _Pool,
    _Records,
    _Store,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    draft_worker._reset_caches()


@pytest.fixture
def purges(monkeypatch):
    calls: list[dict] = []

    def _schedule(pocket_id, **kw):
        calls.append({"pocket_id": pocket_id, **kw})

    monkeypatch.setattr(draft_worker, "schedule_purge", _schedule)
    return calls


async def _publish(monkeypatch, pocket_id: str):
    site_id = str(sites_service._live_object_id("ws1", pocket_id))
    store = _Store()
    content_hash = project_build.project_content_hash(SOURCE)
    store.dist[(pocket_id, project_build.bundle_key(content_hash))] = _bundle(WORKER_MANIFEST)
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")

    async def _deploy_bundle(cf, *, script_name, build_dir, **_kw):
        assert json.loads((Path(build_dir) / "paw-build.json").read_text())
        return SimpleNamespace(script_name=script_name, modules=1, assets=1, warnings=[])

    from pocketpaw_ee.sites import bundle_deploy

    monkeypatch.setattr(bundle_deploy, "deploy_bundle", _deploy_bundle)

    async def _enable(name):
        return None

    async def _subdomain():
        return "acct-sub"

    cf = SimpleNamespace(enable_workers_dev=_enable, workers_dev_subdomain=_subdomain)
    doc = await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key="k",
        site_name="Proj",
        ripple_spec=None,
        theme={},
        engine="project",
        source=dict(SOURCE),
        pattern="landing",
        cloudflare=cf,
    )
    return doc, content_hash


async def test_a_successful_publish_purges_the_pockets_drafts(beanie_test_db, monkeypatch, purges):
    pocket_id = await _make_project_pocket()
    doc, content_hash = await _publish(monkeypatch, pocket_id)
    assert doc.deployed is True
    assert purges == [
        {
            "pocket_id": pocket_id,
            "workspace_id": "ws1",
            "reason": "published",
            "published_hash": content_hash,
            "published_url": doc.url,
        }
    ]


async def test_a_failing_cleanup_never_fails_the_publish(beanie_test_db, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(draft_worker, "schedule_purge", _boom)
    pocket_id = await _make_project_pocket()
    doc, _hash = await _publish(monkeypatch, pocket_id)
    assert doc.deployed is True


async def test_publish_with_the_flag_off_leaves_drafts_alone(beanie_test_db, monkeypatch, purges):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    pocket_id = await _make_project_pocket()
    await _publish(monkeypatch, pocket_id)
    assert purges == []


async def test_the_published_content_shows_the_live_site_not_a_new_draft(
    beanie_test_db, monkeypatch
):
    pocket_id = await _make_project_pocket()
    registry = draft_worker.MemoryRegistry()
    registry.rows[pocket_id] = draft_worker.DraftRecord(
        pocket_id=pocket_id,
        state="purged",
        published_hash=project_build.project_content_hash(SOURCE),
        published_url="https://acme.acct.workers.dev",
    )
    monkeypatch.setattr(draft_worker, "default_registry", lambda: registry)
    pool = _Pool()
    art = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(SOURCE), store=_Store(), _pool=pool, _records=_Records()
    )
    assert art["preview_mode"] == "published"
    assert art["preview_url"] == "https://acme.acct.workers.dev"
    assert art["build_status"] == "none"
    assert pool.calls == [], "the published content must not rebuild a draft"

    # An edit (a different hash) builds a fresh draft again.
    edited = {**SOURCE, "src/pages/index.astro": "<h1>Edited</h1>\n"}
    art = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=edited, store=_Store(), _pool=pool, _records=_Records()
    )
    assert art["build_status"] == "queued" and len(pool.calls) == 1


async def test_published_without_a_known_url_is_null_with_the_mode(beanie_test_db, monkeypatch):
    pocket_id = await _make_project_pocket()
    registry = draft_worker.MemoryRegistry()
    registry.rows[pocket_id] = draft_worker.DraftRecord(
        pocket_id=pocket_id,
        state="purged",
        published_hash=project_build.project_content_hash(SOURCE),
    )
    monkeypatch.setattr(draft_worker, "default_registry", lambda: registry)
    art = await sites_service._project_draft_artifact(
        pocket_id=pocket_id, source=dict(SOURCE), store=_Store(), _pool=_Pool(), _records=_Records()
    )
    assert art["preview_mode"] == "published" and art["preview_url"] is None


async def test_site_delete_cascade_removes_the_drafts(monkeypatch):
    calls: list[dict] = []

    async def _purge(pocket_id, **kw):
        calls.append({"pocket_id": pocket_id, **kw})
        return True

    monkeypatch.setattr(draft_worker, "purge_pocket_drafts", _purge)
    site, saves = _CascadeSite(), []
    deps = _Deps()
    await delete_cascade.run_cascade(site=site, deps=deps, save=_saver(saves))
    assert site.delete_ledger[delete_cascade.STEP_DRAFTS] == delete_cascade.OUTCOME_DONE
    assert calls[0]["pocket_id"] == "pk1" and calls[0]["forget"] is True
    assert calls[0]["cf"] is deps.cloudflare


async def test_a_failed_draft_cleanup_does_not_stop_the_site_delete(monkeypatch):
    async def _purge(pocket_id, **kw):
        raise RuntimeError("cloudflare said no")

    monkeypatch.setattr(draft_worker, "purge_pocket_drafts", _purge)
    site = _CascadeSite()
    await delete_cascade.run_cascade(site=site, deps=_Deps(), save=_saver([]))
    assert site.delete_ledger[delete_cascade.STEP_DRAFTS] == delete_cascade.OUTCOME_PARTIAL
    assert list(site.delete_ledger) == list(delete_cascade.CASCADE_STEPS)


async def test_pocket_delete_purges_and_forgets_its_drafts(beanie_test_db, purges):
    pocket_id = await _make_project_pocket()
    await pockets_service.delete(pocket_id, "u1")
    assert purges == [
        {"pocket_id": pocket_id, "workspace_id": "ws1", "reason": "pocket_deleted", "forget": True}
    ]


async def test_the_mongo_registry_round_trips_a_row(beanie_test_db):
    registry = draft_worker.MongoRegistry()
    rec = draft_worker.DraftRecord(
        pocket_id="pk-mongo", script="paw-draft-x", kv_namespaces={"CACHE": "kv1"}
    )
    await registry.put(rec)
    rec.deployed_hash = "h1"
    await registry.put(rec)
    got = await registry.get("pk-mongo")
    assert got is not None
    assert (got.script, got.deployed_hash, got.kv_namespaces) == (
        "paw-draft-x",
        "h1",
        {"CACHE": "kv1"},
    )
    assert [r.pocket_id for r in await registry.all()] == ["pk-mongo"]
    await registry.delete("pk-mongo")
    assert await registry.get("pk-mongo") is None


def test_the_agent_is_told_to_add_draft_values_for_missing_secrets():
    from pocketpaw_ee.agent.mcp_servers import sites_project

    note = sites_project._draft_worker_note(
        {
            "draft_worker_reason": "draft_worker:secrets_missing",
            "draft_secrets_missing": ["RESEND_KEY", "OPENAI_KEY"],
        }
    )
    assert "RESEND_KEY__DRAFT" in note and "OPENAI_KEY__DRAFT" in note
    assert "request_site_secret" in note
    assert sites_project._draft_worker_note({"draft_worker_reason": None}) == ""


async def test_the_mongo_registry_compare_and_set(beanie_test_db):
    registry = draft_worker.MongoRegistry()
    rec = draft_worker.DraftRecord(pocket_id="pk-cas", script="paw-draft-y")
    assert await registry.cas(rec, None) is True and rec.version == 1
    assert await registry.cas(draft_worker.DraftRecord(pocket_id="pk-cas"), None) is False
    rec.deployed_hash = "h1"
    assert await registry.cas(rec, 1) is True and rec.version == 2
    # A purge (unconditional put) bumps the version: the deploy's next CAS loses.
    purge = await registry.get("pk-cas")
    purge.state = "deleting"
    await registry.put(purge)
    rec.state = "live"
    assert await registry.cas(rec, 2) is False
    assert (await registry.get("pk-cas")).state == "deleting"
