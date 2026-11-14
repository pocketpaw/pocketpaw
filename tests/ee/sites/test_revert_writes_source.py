# tests/ee/sites/test_revert_writes_source.py: a version-history Revert changes the
# site itself, not only its history.
#
# ``revert_pocket_version`` / ``POST .../versions/{n}/revert`` writes the target
# version's content into ``pocket.source`` (a ripple site: ``rippleSpec``) with a
# compare-and-set, then records the revert row. Native-artifact, the publish gate,
# publish and the agent all read ``pocket.source``, so a revert that only wrote the
# row showed the reverted site in the builder and published the un-reverted one. An
# edit that lands while the revert runs wins and the revert answers 409. A project
# site whose source is the published content leaves the published view, so the
# builder builds the reverted draft.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import draft_worker, project_build
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions_service

from tests.ee.sites.test_discard_draft import _artifact, _published_pocket, _source
from tests.ee.sites.test_project_engine import SOURCE

pytestmark = pytest.mark.asyncio

FIRST = "<h1>First edit</h1>\n"
SECOND = "<h1>Second edit</h1>\n"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    draft_worker._reset_caches()


async def _edit(pocket_id: str, contents: str, user_id: str = "u1") -> int:
    await pockets_service.set_project_source(
        pocket_id, user_id, writes={"src/pages/index.astro": contents}
    )
    draft = await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None
    return draft.version_no


async def _published_with_two_edits(monkeypatch) -> tuple[str, Any, int]:
    pocket_id, registry = await _published_pocket(monkeypatch)
    first_no = await _edit(pocket_id, FIRST)
    await _edit(pocket_id, SECOND)
    return pocket_id, registry, first_no


async def test_revert_writes_the_target_content_into_the_source(beanie_test_db, monkeypatch):
    pocket_id, _registry, first_no = await _published_with_two_edits(monkeypatch)

    row = await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first_no
    )

    expected = {**SOURCE, "src/pages/index.astro": FIRST}
    assert await _source(pocket_id) == expected
    assert row.content == expected and row.status == "draft"
    assert row.label == f"Revert to v{first_no}"
    draft = await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None and draft.id == row.id


async def test_the_next_edit_keeps_the_revert(beanie_test_db, monkeypatch):
    pocket_id, _registry, first_no = await _published_with_two_edits(monkeypatch)
    await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first_no
    )

    await pockets_service.set_project_source(pocket_id, "u1", writes={"src/extra.ts": "x\n"})

    after = await _source(pocket_id)
    assert after["src/pages/index.astro"] == FIRST
    draft = await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None and draft.content["src/pages/index.astro"] == FIRST


async def test_native_artifact_builds_the_reverted_source(beanie_test_db, monkeypatch):
    pocket_id, _registry, first_no = await _published_with_two_edits(monkeypatch)
    await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first_no
    )
    seen: list[dict] = []

    async def _capture(**kw):
        seen.append(dict(kw["source"]))
        return {"pocket_id": pocket_id}

    async def _no_config(*_a, **_k):
        return None

    monkeypatch.setattr(sites_service, "_project_draft_artifact", _capture)
    monkeypatch.setattr(sites_service, "_project_site_config", _no_config)

    await sites_service.get_native_artifact(workspace_id="ws1", user_id="u1", pocket_id=pocket_id)

    assert seen == [{**SOURCE, "src/pages/index.astro": FIRST}]


async def test_publish_deploys_the_reverted_source(beanie_test_db, monkeypatch):
    pocket_id, _registry, first_no = await _published_with_two_edits(monkeypatch)
    await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first_no
    )
    deployed: list[dict] = []

    async def _publish(**kw):
        deployed.append(dict(kw["source"]))
        return object()

    monkeypatch.setattr(sites_service, "publish", _publish)

    await sites_service.publish_pocket(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, preview=True
    )

    assert deployed == [{**SOURCE, "src/pages/index.astro": FIRST}]


async def test_reverting_to_the_live_content_leaves_the_published_view(beanie_test_db, monkeypatch):
    """The source goes back to exactly what is published. The revert recorded a
    draft, so the builder must build that draft (the open-draft registry mark), not
    answer the published view with no draft at all."""
    pocket_id, registry = await _published_pocket(monkeypatch)
    published = await versions_service.get_published(scope_type="pocket", scope_id=pocket_id)
    assert published is not None
    await _edit(pocket_id, FIRST)

    await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=published.version_no
    )

    assert await _source(pocket_id) == SOURCE
    assert registry.rows[pocket_id].reopened_hash == project_build.project_content_hash(SOURCE)
    assert (await _artifact(pocket_id))["preview_mode"] != "published"


async def test_an_edit_landing_mid_revert_wins_and_the_revert_is_a_409(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import ConflictError

    pocket_id, _registry, first_no = await _published_with_two_edits(monkeypatch)
    racing = "<h1>Agent wrote this meanwhile</h1>\n"
    real_list = versions_service.list_versions

    async def _list_then_race(**kw):
        rows = await real_list(**kw)
        await pockets_service.set_project_source(
            pocket_id, "u1", writes={"src/pages/index.astro": racing}
        )
        return rows

    monkeypatch.setattr(versions_service, "list_versions", _list_then_race)
    before = await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id)

    with pytest.raises(ConflictError) as err:
        await sites_service.revert_pocket_version(
            workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first_no
        )

    assert err.value.code == "sites.revert_conflict"
    assert (await _source(pocket_id))["src/pages/index.astro"] == racing
    # No revert row was recorded: the head draft is the racing edit's.
    draft = await versions_service.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None and draft.id != before.id
    assert draft.content["src/pages/index.astro"] == racing
    assert not draft.label or not draft.label.startswith("Revert to")


async def test_the_revert_route_answers_409_on_a_conflict(beanie_test_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from tests.ee.sites.test_router import _build_app

    pocket_id, _registry = await _published_pocket(monkeypatch, user_id="user-test-1")
    first_no = await _edit(pocket_id, FIRST, user_id="user-test-1")
    await _edit(pocket_id, SECOND, user_id="user-test-1")
    real_list = versions_service.list_versions

    async def _list_then_race(**kw):
        rows = await real_list(**kw)
        await pockets_service.set_project_source(
            pocket_id, "user-test-1", writes={"src/pages/index.astro": "<h1>race</h1>\n"}
        )
        return rows

    monkeypatch.setattr(versions_service, "list_versions", _list_then_race)
    app = _build_app("ws1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/api/v1/sites/by-pocket/{pocket_id}/versions/{first_no}/revert")
    assert resp.status_code == 409, resp.text
    assert "sites.revert_conflict" in resp.text


async def test_revert_needs_edit_access(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.models.pocket import Pocket

    pocket_id, _registry = await _published_pocket(monkeypatch, user_id="someone-else")
    first_no = await _edit(pocket_id, FIRST, user_id="someone-else")
    await _edit(pocket_id, SECOND, user_id="someone-else")
    doc = await Pocket.get(pocket_id)
    doc.visibility = "public"
    await doc.save()

    with pytest.raises(Forbidden):
        await sites_service.revert_pocket_version(
            workspace_id="ws1", user_id="viewer", pocket_id=pocket_id, version_no=first_no
        )
    assert (await _source(pocket_id, user_id="someone-else"))["src/pages/index.astro"] == SECOND


async def test_revert_of_a_ripple_site_writes_the_spec(beanie_test_db):
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Spec",
        type_="site",
        pattern="landing",
        ripple_spec={"ui": [{"type": "text", "text": "one"}]},
        engine="ripple",
        trusted=True,
    )
    assert err is None, err
    first = await versions_service.write_draft(
        scope_type="pocket",
        scope_id=pocket_id,
        workspace_id="ws1",
        content=dict((await pockets_service.get(pocket_id, "u1"))["rippleSpec"]),
    )
    await pockets_service.merge_spec(
        "ws1", "u1", pocket_id, {"replace": {"ui": [{"type": "text", "text": "two"}]}}
    )
    assert (await pockets_service.get(pocket_id, "u1"))["rippleSpec"] != first.content

    await sites_service.revert_pocket_version(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id, version_no=first.version_no
    )

    assert (await pockets_service.get(pocket_id, "u1"))["rippleSpec"] == first.content


async def test_the_revert_route_is_a_404_for_another_workspace(beanie_test_db, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from tests.ee.sites.test_router import _build_app

    pocket_id, _registry = await _published_pocket(monkeypatch, user_id="user-test-1")
    first_no = await _edit(pocket_id, FIRST, user_id="user-test-1")
    await _edit(pocket_id, SECOND, user_id="user-test-1")
    app = _build_app("ws-other")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/api/v1/sites/by-pocket/{pocket_id}/versions/{first_no}/revert")
    assert resp.status_code == 404, resp.text
    assert (await _source(pocket_id, user_id="user-test-1"))["src/pages/index.astro"] == SECOND
