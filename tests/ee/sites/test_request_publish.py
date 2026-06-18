# tests/ee/sites/test_request_publish.py
# Created: 2026-06-18 (feat/branch-primitive-revert-history, BP-4) — coverage for
# the two Branch-primitive Sites surfaces over the BP-1 versions spine:
#   Part B — version_history() / GET /sites/by-pocket/{id}/versions returns the
#            ordered (oldest → newest) version timeline for a pocket, tenant-scoped.
#   Part C — request_publish_pocket() / POST /sites/by-pocket/{id}/request-publish
#            builds a WELL-FORMED _artifact_change review proposal server-side (the
#            client never hand-builds the Instinct propose), stamps a real
#            workspace, and returns the created Action; a pocket with no draft → 400.
#
# The headline test is the ROUND-TRIP: request-publish creates the gate Action,
# approving it (through the REAL Instinct router + the REAL BP-3 merge executor,
# with only the site DEPLOY faked) publishes the reviewed draft version. This pins
# that request-publish → approve → published works end to end through the gate.
from __future__ import annotations

import pytest
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions

from pocketpaw.instinct.store import InstinctStore

pytestmark = pytest.mark.asyncio

WS = "ws-rp"
POCKET = "pocket-rp"
USER = "user-rp"


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    """A throwaway InstinctStore wired into BOTH places the code reads it from:
    the sites service (``pocketpaw.stores.get_instinct_store``) and the BP-3
    executor (same accessor). Returns the store so a test can read the Action
    back."""
    store = InstinctStore(tmp_path / "rp_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: store)
    return store


# ---------------------------------------------------------------------------
# Part B — version_history (ordered timeline, tenant-scoped)
# ---------------------------------------------------------------------------


async def test_version_history_oldest_to_newest(beanie_test_db):
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 2}
    )
    rows = await sites_service.version_history(workspace_id=WS, pocket_id=POCKET)
    assert [r.version_no for r in rows] == [1, 2]  # oldest → newest


async def test_version_history_is_tenant_scoped(beanie_test_db):
    await versions.write_draft(scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={})
    # A version of the SAME pocket id stamped under another workspace must not leak.
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id="ws-OTHER", content={}
    )
    rows = await sites_service.version_history(workspace_id=WS, pocket_id=POCKET)
    assert all(r.workspace_id == WS for r in rows)
    assert len(rows) == 1


async def test_version_history_empty_for_unversioned_pocket(beanie_test_db):
    rows = await sites_service.version_history(workspace_id=WS, pocket_id="never-versioned")
    assert rows == []


# ---------------------------------------------------------------------------
# Part C — request_publish_pocket builds a well-formed proposal
# ---------------------------------------------------------------------------


async def test_request_publish_builds_well_formed_proposal(beanie_test_db, instinct_store):
    """The created Action carries an _artifact_change blob with the exact shape
    BP-3's gate + executor read: scope_type/scope_id/branch/from/to/workspace."""
    # A published version is live; a newer draft is the working copy to review.
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "live"}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "draft"}
    )

    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET
    )

    assert action.status.value == "pending"
    assert action.scope_type == "pocket"
    blob = action.parameters["_artifact_change"]
    assert blob["scope_type"] == "pocket"
    assert blob["scope_id"] == POCKET
    assert blob["branch"] == "main"
    assert blob["from_version_id"] == str(v1.id)  # the currently-published version
    assert blob["to_version_id"] == str(v2.id)  # the current draft
    assert blob["workspace"] == WS  # real workspace, never empty (BP-3 hard-403s "")
    assert blob["user_id"] == USER


async def test_request_publish_first_publish_has_null_from(beanie_test_db, instinct_store):
    """Nothing published yet → from_version_id is None (a first publish)."""
    draft = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "draft"}
    )
    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET
    )
    blob = action.parameters["_artifact_change"]
    assert blob["from_version_id"] is None
    assert blob["to_version_id"] == str(draft.id)


async def test_request_publish_no_draft_raises(beanie_test_db, instinct_store):
    """A pocket with no draft version → ValueError (the router maps it to 400)."""
    with pytest.raises(ValueError):
        await sites_service.request_publish_pocket(
            workspace_id=WS, user_id=USER, pocket_id="no-draft-here"
        )


async def test_request_publish_ignores_foreign_workspace_draft(beanie_test_db, instinct_store):
    """A draft under another workspace is 'nothing here' for this caller — the
    tenant guard treats it as no draft → ValueError."""
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id="ws-OTHER", content={"v": 1}
    )
    with pytest.raises(ValueError):
        await sites_service.request_publish_pocket(workspace_id=WS, user_id=USER, pocket_id=POCKET)


# ---------------------------------------------------------------------------
# The round-trip — request-publish → approve (real gate + real merge executor)
# → the reviewed draft version is published.
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str, workspace_id: str) -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


async def test_request_publish_approve_publishes_the_draft(
    beanie_test_db, instinct_store, monkeypatch
):
    """End to end: request-publish creates the gate Action; approving it through
    the REAL Instinct router + the REAL BP-3 merge executor publishes the reviewed
    draft version (only the site DEPLOY is faked). Pins request-publish → approve
    → published."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.instinct.router import router as instinct_router

    # A published v1 is live; a newer draft v2 is the working copy to review.
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "live"}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "draft"}
    )

    # 1. request-publish — the server builds the gate proposal.
    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET
    )
    assert action.parameters["_artifact_change"]["to_version_id"] == str(v2.id)

    # The merge executor triggers a site DEPLOY for a pocket scope — fake just the
    # deploy so no Bun/workerd/CF is touched; the version publish below is REAL.
    deploy_calls: dict = {}

    async def _fake_deploy(*, workspace_id, user_id, pocket_id):
        deploy_calls["pocket_id"] = pocket_id

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_deploy)

    # 2. approve the Action through the REAL Instinct router. The gate's
    # cross-workspace check passes (caller's active workspace == blob workspace),
    # then it dispatches the real BP-3 merge executor (publish v2 + deploy).
    from unittest.mock import AsyncMock

    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    user = _FakeUser(USER, WS)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(instinct_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace

    from unittest.mock import patch

    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        client = TestClient(app)
        resp = client.post(f"/instinct/actions/{action.id}/approve")
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"]["status"] == "approved"

    # 3. The reviewed draft (v2) is now the published version — request-publish →
    # approve → published worked through the gate.
    live = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert live is not None
    assert live.id == v2.id
    assert live.content == {"v": "draft"}
    # The deploy fired for this pocket.
    assert deploy_calls.get("pocket_id") == POCKET
