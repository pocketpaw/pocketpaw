# tests/ee/sites/test_discard_draft.py: Discard throws away a pocket's unpublished
# changes for real. ``discard_pocket_draft`` / ``POST .../discard-draft`` puts
# ``pocket.source`` back to the published content, so native-artifact answers the
# published view again and ``/status`` reports no unpublished changes; it marks the
# draft rows discarded, rejects a pending publish review of the pocket, and refuses
# (409) when nothing was ever published, so there is no live version to go back to.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import draft_worker, project_build
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions_service

from pocketpaw.instinct.store import InstinctStore
from tests.ee.sites.test_project_engine import SOURCE, _make_project_pocket, _Pool, _Records, _Store

pytestmark = pytest.mark.asyncio

EDITED = "<h1>Discard Me Heading</h1>\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    draft_worker._reset_caches()


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    store = InstinctStore(tmp_path / "discard_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


async def _published_pocket(monkeypatch, user_id: str = "u1") -> tuple[str, Any]:
    """A project pocket whose SOURCE is published: a published version row and a
    purged registry row that remembers the published hash (what a publish leaves)."""
    pocket_id = await _make_project_pocket(user_id=user_id)
    row = await versions_service.write_draft(
        scope_type="pocket", scope_id=pocket_id, workspace_id="ws1", content=dict(SOURCE)
    )
    await versions_service.publish(
        scope_type="pocket", scope_id=pocket_id, workspace_id="ws1", version_id=str(row.id)
    )
    registry = draft_worker.MemoryRegistry()
    registry.rows[pocket_id] = draft_worker.DraftRecord(
        pocket_id=pocket_id,
        state="purged",
        published_hash=project_build.project_content_hash(SOURCE),
        published_url="https://acme.acct.workers.dev",
    )
    monkeypatch.setattr(draft_worker, "default_registry", lambda: registry)
    return pocket_id, registry


async def _edit(pocket_id: str, user_id: str = "u1") -> None:
    await pockets_service.set_project_source(
        pocket_id, user_id, writes={"src/pages/index.astro": EDITED}
    )


async def _source(pocket_id: str, user_id: str = "u1") -> dict:
    return dict((await pockets_service.get(pocket_id, user_id))["source"])


async def _artifact(pocket_id: str) -> dict:
    return await sites_service._project_draft_artifact(
        pocket_id=pocket_id,
        source=await _source(pocket_id),
        store=_Store(),
        _pool=_Pool(),
        _records=_Records(),
    )


async def test_discard_puts_the_source_back_to_the_published_content(beanie_test_db, monkeypatch):
    pocket_id, _registry = await _published_pocket(monkeypatch)
    await _edit(pocket_id)
    assert (await _source(pocket_id))["src/pages/index.astro"] == EDITED
    assert (await _artifact(pocket_id))["preview_mode"] != "published"

    out = await sites_service.discard_pocket_draft(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )

    assert out["restored"] is True
    assert out["discarded_versions"] >= 1
    assert await _source(pocket_id) == SOURCE
    art = await _artifact(pocket_id)
    assert art["preview_mode"] == "published"
    assert art["preview_url"] == "https://acme.acct.workers.dev"
    assert await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id) is None
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)
    assert status.has_unpublished_changes is False


async def test_discard_after_open_draft_returns_to_the_published_view(beanie_test_db, monkeypatch):
    pocket_id, registry = await _published_pocket(monkeypatch)
    out = await sites_service.open_pocket_draft(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )
    assert out["opened"] is True
    await _edit(pocket_id)

    await sites_service.discard_pocket_draft(workspace_id="ws1", user_id="u1", pocket_id=pocket_id)

    assert registry.rows[pocket_id].reopened_hash == ""
    assert (await _artifact(pocket_id))["preview_mode"] == "published"


async def test_discard_rejects_a_pending_publish_review(
    beanie_test_db, monkeypatch, instinct_store
):
    pocket_id, _registry = await _published_pocket(monkeypatch)
    await _edit(pocket_id)
    action = await sites_service.request_publish_pocket(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )

    out = await sites_service.discard_pocket_draft(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )

    assert out["rejected_reviews"] == 1
    after = await instinct_store.get_action(action.id)
    assert after is not None and after.status.value == "rejected"


async def test_discard_is_idempotent(beanie_test_db, monkeypatch):
    pocket_id, _registry = await _published_pocket(monkeypatch)
    out = await sites_service.discard_pocket_draft(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )
    assert out["restored"] is False and out["discarded_versions"] == 0
    assert await _source(pocket_id) == SOURCE


async def test_discard_refuses_when_nothing_was_ever_published(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import ConflictError

    pocket_id = await _make_project_pocket()
    await _edit(pocket_id)
    with pytest.raises(ConflictError) as err:
        await sites_service.discard_pocket_draft(
            workspace_id="ws1", user_id="u1", pocket_id=pocket_id
        )
    assert err.value.code == "sites.discard_nothing_live"
    # Refused means untouched: the edit and its draft row are still there.
    assert (await _source(pocket_id))["src/pages/index.astro"] == EDITED
    assert await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id) is not None


# ---------------------------------------------------------------- route


async def _post_discard(pocket_id: str, workspace_id: str = "ws1"):
    from httpx import ASGITransport, AsyncClient

    from tests.ee.sites.test_router import _build_app

    app = _build_app(workspace_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/api/v1/sites/by-pocket/{pocket_id}/discard-draft")


async def test_the_discard_route_restores_the_published_content(beanie_test_db, monkeypatch):
    pocket_id, _registry = await _published_pocket(monkeypatch, user_id="user-test-1")
    await _edit(pocket_id, user_id="user-test-1")
    resp = await _post_discard(pocket_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == pocket_id and body["restored"] is True
    assert await _source(pocket_id, user_id="user-test-1") == SOURCE


async def test_the_discard_route_is_a_404_for_another_workspace(beanie_test_db, monkeypatch):
    pocket_id, _registry = await _published_pocket(monkeypatch, user_id="user-test-1")
    resp = await _post_discard(pocket_id, workspace_id="ws-other")
    assert resp.status_code == 404, resp.text


async def test_the_discard_route_says_why_it_refuses(beanie_test_db, monkeypatch):
    pocket_id = await _make_project_pocket(user_id="user-test-1")
    resp = await _post_discard(pocket_id)
    assert resp.status_code == 409, resp.text
    assert "published" in resp.text
