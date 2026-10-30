# tests/ee/sites/test_kb_sync_triggers.py — the two automatic triggers that keep a
# concierge's knowledge current.
# Created 2026-07-26. The sync itself is covered in tests/cloud/test_site_kb_ingest.py;
# this file only proves it is FIRED at the right moments and not at the wrong ones:
#   * a live publish schedules one (the site's content just changed, so what the
#     concierge can quote is now stale);
#   * a PREVIEW publish does not (a draft must never rewrite the live KB);
#   * provisioning a dedicated agent schedules one, which is how a site published
#     before this existed stops being knowledge-empty;
#   * neither trigger can fail its caller — publishing and binding an agent both
#     survive a broken sync.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites import service as sites_service


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


@pytest.fixture
def captured_syncs(monkeypatch):
    """Record the sites the service schedules a knowledge sync for, without running
    one (a real sync would shell out to kb-go)."""
    seen: list = []
    monkeypatch.setattr(sites_service, "_schedule_site_knowledge_sync", seen.append)
    return seen


async def _publish(*, preview: bool = False):
    return await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pocket-1",
        ripple_spec={"type": "container"},
        theme={},
        name="Brew and Co",
        preview=preview,
        _generator=_FakeGenerator(),
        _cloudflare=None if preview else _FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=(lambda site_id, project_dir: f"http://localhost/{site_id}"),
    )


@pytest.mark.asyncio
async def test_live_publish_schedules_a_knowledge_sync(beanie_test_db, captured_syncs):
    """A publish changes what the site says, so the concierge's knowledge is stale
    the moment it lands."""
    await _publish()
    assert len(captured_syncs) == 1
    assert captured_syncs[0].pocket_id == "pocket-1"


@pytest.mark.asyncio
async def test_preview_publish_does_not_touch_the_live_knowledge(
    monkeypatch, beanie_test_db, captured_syncs
):
    """A preview is a draft the owner has not approved. Ingesting it would let the
    concierge quote a page no visitor can see."""
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    await _publish(preview=True)
    assert captured_syncs == []


@pytest.mark.asyncio
async def test_publish_survives_a_broken_sync_scheduler(beanie_test_db, monkeypatch):
    """The sync fires from the tail of a LIVE deploy, so anything escaping it would
    fail a publish of a site that is already deployed and serving. A concierge with
    stale knowledge is a far smaller problem than that."""
    from pocketpaw_ee.sites import kb_ingest

    def _boom(coro):
        coro.close()
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(kb_ingest, "_default_sync_scheduler", _boom)

    site = await _publish()

    assert site.deployed is True


@pytest.mark.asyncio
async def test_agent_provisioning_schedules_a_knowledge_sync(monkeypatch):
    """A dedicated agent is provisioned with a soul and an empty KB. Sites published
    before this existed have never synced, so a bind is the catch-up point."""
    from pocketpaw_ee.paw_bar import agent_provisioning

    seen: list = []
    monkeypatch.setattr(agent_provisioning, "_schedule_knowledge_sync", seen.append)

    site = type(
        "S",
        (),
        {
            "id": "site-1",
            "pocket_id": "pocket-1",
            "workspace": "ws1",
            "owner": "u1",
            "name": "Brew and Co",
            "concierge_greeting": "",
        },
    )()
    widget = type("W", (), {"id": "w1", "agent_id": "", "spec": None})()
    agent = type("A", (), {"id": "agent-1"})()

    async def _get_by_slug(workspace_id, slug):
        return agent

    class _Store:
        async def update_fields(self, widget_id, fields, *, workspace_id):
            return widget

    monkeypatch.setattr(agent_provisioning, "_store", lambda: _Store())
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_by_slug", _get_by_slug, raising=False
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.legacy_ctx", lambda *a, **k: object(), raising=False
    )

    bound = await agent_provisioning.ensure_site_agent(site, widget)

    assert bound == "agent-1"
    assert seen == [site]


@pytest.mark.asyncio
async def test_provisioning_sync_helper_never_raises(monkeypatch):
    """``_schedule_knowledge_sync`` is the failure-soft boundary: a bind must
    complete even if the sites KB module cannot be reached at all."""
    from pocketpaw_ee.paw_bar import agent_provisioning
    from pocketpaw_ee.sites import kb_ingest

    def _boom(site):
        raise RuntimeError("kb module down")

    monkeypatch.setattr(kb_ingest, "schedule_site_knowledge_sync", _boom)
    agent_provisioning._schedule_knowledge_sync(object())  # must not raise
